#!/usr/bin/python
#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#
# Monitors Docker containers for CPU and memory usage, and
# prepares an HTML timeline based on said monitoring.
#
# Usage example:
#   mon = monitor.ContainerMonitor("monitoring.txt")
#   mon.start()
#   # container1 is an object with attributes id, name, and logfile.
#   mon.add(container1)
#   mon.add(container2)
#   mon.stop()
#   timeline = monitor.Timeline("monitoring.txt",
#       [container1, container2],
#       re.compile(">>> "))
#   timeline.create("output.html")

from __future__ import absolute_import, division, print_function
import datetime
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import xml.etree.ElementTree as ET


# Unit for reporting user/system CPU seconds in cpuacct.stat.
# See https://www.kernel.org/doc/Documentation/cgroup-v1/cpuacct.txt and time(7).
USER_HZ = os.sysconf(os.sysconf_names['SC_CLK_TCK'])


def total_memory():
  """Returns total RAM on system, in GB."""
  return _memory()[0]


def used_memory():
  """Returns total used RAM on system, in GB."""
  return _memory()[1]


def _host_cpu():
  """Get host CPU usage as percentages (user, system, idle)."""
  try:
    with open("/proc/stat") as f:
      first_line = f.readline()

    # Parse: cpu user nice system idle iowait irq softirq steal guest guest_nice
    fields = first_line.split()
    if len(fields) >= 8 and fields[0] == "cpu":
      user = int(fields[1])
      nice = int(fields[2])
      system = int(fields[3])
      idle = int(fields[4])
      iowait = int(fields[5])
      irq = int(fields[6])
      softirq = int(fields[7])

      # Total CPU time
      total = user + nice + system + idle + iowait + irq + softirq
      if total > 0:
        user_pct = ((user + nice) * 100.0) / total
        system_pct = ((system + irq + softirq) * 100.0) / total
        idle_pct = ((idle + iowait) * 100.0) / total
        return user_pct, system_pct, idle_pct
  except (IOError, ValueError, IndexError) as e:
    logging.warning("Failed to get host CPU usage: %s", e)

  return 0.0, 0.0, 100.0


def _memory():
  """Get host memory usage in GB."""
  with open("/proc/meminfo") as f:
    meminfo = f.read()

  total_kb = None
  available_kb = None

  for line in meminfo.split("\n"):
    if line.startswith("MemTotal:"):
      total_kb = int(line.split()[1])
    elif line.startswith("MemAvailable:"):
      available_kb = int(line.split()[1])

  if total_kb and available_kb:
    total_gb = total_kb / (1024.0 * 1024.0)
    used_gb = (total_kb - available_kb) / (1024.0 * 1024.0)
    return total_gb, used_gb

  return 0.0, 0.0


def _global_disk_usage():
  """Returns (total, used) disk space for root filesystem, in GB."""
  try:
    df_output = subprocess.check_output(["df", "-B1", "/"], universal_newlines=True)
    lines = df_output.strip().split('\n')
    if len(lines) >= 2:
      # Parse df output: Filesystem 1B-blocks Used Available Use% Mounted
      fields = lines[1].split()
      if len(fields) >= 4:
        total_bytes = int(fields[1])
        used_bytes = int(fields[2])
        total_gb = total_bytes / (1024.0 * 1024.0 * 1024.0)
        used_gb = used_bytes / (1024.0 * 1024.0 * 1024.0)
        return (total_gb, used_gb)
  except Exception as e:
    logging.warning("Could not get global disk usage: %s", e)
  return (0.0, 0.0)


def _container_disk_usage(container_id):
  """Get disk usage for a specific container in MB."""
  try:
    # First check if container still exists to avoid noisy error messages
    try:
      with open(os.devnull, 'w') as devnull:
        subprocess.check_output(['docker', 'container', 'inspect', container_id,
                               '--format={{.State.Running}}'], stderr=devnull)
    except subprocess.CalledProcessError:
      # Container doesn't exist anymore - this is normal during cleanup
      return 0.0
    # Use docker system df to get container size information
    with open(os.devnull, 'w') as devnull:
      result = subprocess.check_output(['docker', 'container', 'inspect', container_id,
                                       '--format={{.SizeRw}}'], stderr=devnull)
    size_str = result.strip()
    # If SizeRw is not available, try alternative method
    if not size_str or size_str == '<nil>' or size_str == '0':
      # For running containers, try to get disk usage from inside
      try:
        with open(os.devnull, 'w') as devnull:
          result = subprocess.check_output(['docker', 'exec', container_id, 'df', '/'],
                                         universal_newlines=True, stderr=devnull)
        lines = result.strip().split('\n')
        if len(lines) > 1:  # Skip header line
          fields = lines[1].split()
          if len(fields) >= 3:
            used_kb = int(fields[2])
            return used_kb / 1024.0  # Convert KB to MB
      except subprocess.CalledProcessError:
        pass  # Container might not be running or exec might fail

      return 0.0
    # Parse size (could be in bytes)
    try:
      size_bytes = int(size_str)
      return size_bytes / (1024.0 * 1024.0)  # Convert bytes to MB
    except ValueError:
      return 0.0
  except subprocess.CalledProcessError:
    # Container no longer exists - this is expected during cleanup, so don't log
    pass
  except (ValueError, IndexError) as e:
    logging.debug("Failed to parse container disk usage for %s: %s", container_id, e)
  return 0.0


def datetime_to_seconds_since_epoch(dt):
  """Converts a Python datetime to seconds since the epoch."""
  return time.mktime(dt.timetuple())


def split_timestamp(line):
  """Parses timestamp at beginning of a line.

  Returns a tuple of seconds since the epoch and the rest
  of the line. Returns None on parse failures.
  """
  LENGTH = 26
  FORMAT = "%Y-%m-%d %H:%M:%S.%f"
  t = line[:LENGTH]
  return (datetime_to_seconds_since_epoch(datetime.datetime.strptime(t, FORMAT)),
          line[LENGTH + 1:])


class ContainerMonitor(object):
  """Monitors Docker containers.

  Monitoring data is written to a file. An example is:

  2018-02-02 09:01:37.143591 d8f640989524be3939a70557a7bf7c015ba62ea5a105a64c94472d4ebca93c50 cpu user 2 system 5
  2018-02-02 09:01:37.143591 d8f640989524be3939a70557a7bf7c015ba62ea5a105a64c94472d4ebca93c50 memory cache 11481088 rss 4009984 rss_huge 0 mapped_file 8605696 dirty 24576 writeback 0 pgpgin 4406 pgpgout 624 pgfault 3739 pgmajfault 99 inactive_anon 0 active_anon 3891200 inactive_file 7614464 active_file 3747840 unevictable 0 hierarchical_memory_limit 9223372036854771712 total_cache 11481088 total_rss 4009984 total_rss_huge 0 total_mapped_file 8605696 total_dirty 24576 total_writeback 0 total_pgpgin 4406 total_pgpgout 624 total_pgfault 3739 total_pgmajfault 99 total_inactive_anon 0 total_active_anon 3891200 total_inactive_file 7614464 total_active_file 3747840 total_unevictable 0

  That is, the format is:

  <timestamp> <container> cpu user <usercpu> system <systemcpu>
  <timestamp> <container> memory <contents of memory.stat without newlines>

  <usercpu> and <systemcpu> are in the units of USER_HZ.
  See https://www.kernel.org/doc/Documentation/cgroup-v1/memory.txt for documentation
  on memory.stat; it's in the "memory" cgroup, often mounted at
  /sys/fs/cgroup/memory/<cgroup>/memory.stat.

  This format is parsed back by the Timeline class below and should
  not be considered an API.
  """

  def __init__(self, output_path, frequency_seconds=1):
    """frequency_seconds is how often metrics are gathered"""
    self.containers = []
    self.output_path = output_path
    self.keep_monitoring = None
    self.monitor_thread = None
    self.frequency_seconds = frequency_seconds
    self.min_memory_usage_gb = None
    self.max_memory_usage_gb = None

  def start(self):
    self.keep_monitoring = True
    self.monitor_thread = threading.Thread(target=self._monitor)
    self.monitor_thread.setDaemon(True)
    self.monitor_thread.start()

  def stop(self):
    self.keep_monitoring = False
    self.monitor_thread.join()

  def add(self, container):
    """Adds monitoring for container, which is an object with property 'id'."""
    self.containers.append(container)

  @staticmethod
  def _metrics_from_stat_file(root, container, stat):
    """Returns metrics stat file contents.

    root: a cgroups root (a path as a string)
    container: an object with string attribute id
    stat: a string filename

    Returns contents of <root>/<container.id>/<stat>
    with newlines replaced with spaces.
    Returns None on errors.
    """
    dirname = os.path.join(root, "docker", container.id)
    if not os.path.isdir(dirname):
      # Container may no longer exist.
      return None
    try:
      statcontents = open(os.path.join(dirname, stat)).read()
      return statcontents.replace("\n", " ").strip()
    except IOError as e:
      # Ignore errors; cgroup can disappear on us.
      logging.warning("Ignoring exception reading cgroup. " +
                      "This can happen if container just exited. " + str(e))
      return None

  def _monitor(self):
    """Monitors CPU and memory usage of containers, supporting both cgroup v1 and v2."""
    # Detect cgroup version
    cgroup_v2_path = "/sys/fs/cgroup"
    cgroup_v1_cpu = None
    cgroup_v1_mem = None
    cgroup_v2 = False
    try:
      # v2: unified hierarchy has cgroup.controllers file
      if os.path.exists(os.path.join(cgroup_v2_path, "cgroup.controllers")):
        cgroup_v2 = True
    except Exception:
      pass

    if not cgroup_v2:
      # Try v1 detection
      try:
        all_cgroups = subprocess.check_output(
            "findmnt -n -o TARGET -t cgroup --source cgroup".split(),
            universal_newlines=True
        ).split("\n")
        cgroup_v1_cpu = next((c for c in all_cgroups if "cpuacct" in c), None)
        cgroup_v1_mem = next((c for c in all_cgroups if "memory" in c), None)
      except Exception as e:
        logging.warning("Could not detect cgroup v1 mounts: %s", e)

    if cgroup_v2:
      logging.info("Detected cgroup v2 at %s", cgroup_v2_path)
    elif cgroup_v1_cpu and cgroup_v1_mem:
      logging.info("Using cgroup v1: cpuacct %s, memory %s", cgroup_v1_cpu, cgroup_v1_mem)
    else:
      logging.warning("No usable cgroup mounts found; resource monitoring disabled.")
      return

    self.min_memory_usage_gb = None
    self.max_memory_usage_gb = None

    def get_v2_metrics(container):
      # v2: Try multiple possible paths for container cgroups
      possible_paths = [
        os.path.join(cgroup_v2_path, "docker", container.id),  # cgroupfs driver
        os.path.join(cgroup_v2_path, "system.slice",
                     "docker-{}.scope".format(container.id)),  # systemd driver
      ]

      dirname = None
      for path in possible_paths:
        if os.path.isdir(path):
          dirname = path
          break

      if not dirname:
        logging.debug("No cgroup directory found for container %s. Tried: %s",
                      container.id, possible_paths)
        return None, None

      logging.debug("Found cgroup v2 path: %s", dirname)

      try:
        cpu_file = os.path.join(dirname, "cpu.stat")
        cpu_stat = open(cpu_file).read().replace("\n", " ").strip()
        logging.debug("CPU stats: %s", cpu_stat[:100])
      except Exception as e:
        logging.debug("Failed to read CPU stats: %s", e)
        cpu_stat = None
      try:
        mem_file = os.path.join(dirname, "memory.stat")
        mem_stat = open(mem_file).read().replace("\n", " ").strip()
        logging.debug("Memory stats length: %d", len(mem_stat))
      except Exception as e:
        logging.debug("Failed to read memory stats: %s", e)
        mem_stat = None
      return cpu_stat, mem_stat

    with open(self.output_path, "w") as output:
      while self.keep_monitoring:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
        logging.debug("Monitoring %d containers at %s", len(self.containers), now)
        for c in self.containers:
          logging.debug("Processing container: %s", c.id)
          if cgroup_v2:
            cpu, memory = get_v2_metrics(c)
            # v2 cpu.stat: usage_usec, user_usec, system_usec
            # For compatibility, we fake v1 format: user <user_usec> system <system_usec>
            if cpu:
              try:
                # Parse cgroup v2 format: "key1 value1 key2 value2 key3 value3"
                cpu_parts = cpu.split()
                cpu_fields = {}
                for i in range(0, len(cpu_parts), 2):
                  if i + 1 < len(cpu_parts):
                    cpu_fields[cpu_parts[i]] = cpu_parts[i + 1]
                user_usec = int(cpu_fields.get("user_usec", 0))
                system_usec = int(cpu_fields.get("system_usec", 0))
                output.write("%s %s cpu user %d system %d\n" % (
                    now, c.id, user_usec // 1000000, system_usec // 1000000))
                logging.debug("Wrote CPU metrics for %s", c.id)
              except Exception as e:
                logging.warning("Failed to parse cgroup v2 cpu.stat: %s", e)
            if memory:
              output.write("%s %s memory %s\n" % (now, c.id, memory))
              logging.debug("Wrote memory metrics for %s", c.id)
          else:
            cpu = self._metrics_from_stat_file(cgroup_v1_cpu, c, "cpuacct.stat")
            memory = self._metrics_from_stat_file(cgroup_v1_mem, c, "memory.stat")
            if cpu:
              output.write("%s %s cpu %s\n" % (now, c.id, cpu))
            if memory:
              output.write("%s %s memory %s\n" % (now, c.id, memory))

          # Write container disk usage metrics
          container_disk_mb = _container_disk_usage(c.id)
          output.write("%s %s disk used_mb %.2f\n" % (now, c.id, container_disk_mb))
          logging.debug("Wrote disk metrics for %s: %.2f MB", c.id, container_disk_mb)
        # Write host memory, disk, and CPU metrics
        host_memory_total, host_memory_used = _memory()
        host_disk_total, host_disk_used = _global_disk_usage()
        host_cpu_user, host_cpu_system, host_cpu_idle = _host_cpu()
        output.write("%s HOST memory total_gb %.3f used_gb %.3f\n" % (
            now, host_memory_total, host_memory_used))
        output.write("%s HOST disk total_gb %.3f used_gb %.3f\n" % (
            now, host_disk_total, host_disk_used))
        output.write("%s HOST cpu user_pct %.3f system_pct %.3f idle_pct %.3f\n" % (
            now, host_cpu_user, host_cpu_system, host_cpu_idle))
        logging.debug("Wrote host metrics: Memory %.3f/%.3f GB, Disk %.3f/%.3f GB, "
                      "CPU %.1f%% user %.1f%% system",
                      host_memory_used, host_memory_total, host_disk_used,
                      host_disk_total, host_cpu_user, host_cpu_system)
        output.flush()

        m = used_memory()
        if self.min_memory_usage_gb is None:
          self.min_memory_usage_gb, self.max_memory_usage_gb = m, m
        else:
          self.min_memory_usage_gb = min(self.min_memory_usage_gb, m)
          self.max_memory_usage_gb = max(self.max_memory_usage_gb, m)
        time.sleep(self.frequency_seconds)


class Timeline(object):
  """Given metric and log data for containers, creates a timeline report.

  This is a standalone HTML file with a timeline for the log files and CPU charts for
  the containers. The HTML uses https://developers.google.com/chart/ for rendering
  the charts, which happens in the browser.
  """

  def __init__(self, monitor_file, containers, interesting_re, buildname):
    self.monitor_file = monitor_file
    self.containers = containers
    self.interesting_re = interesting_re
    self.buildname = buildname

  def logfile_timeline(self, container):
    """Returns a list of (name, timestamp, line) tuples for interesting lines in
    the container's logfile. container is expected to have name and logfile attributes.
    """
    interesting_lines = [
        line.strip()
        for line in open(container.logfile)
        if self.interesting_re.search(line)]
    return [(container.name,) + split_timestamp(line) for line in interesting_lines]

  def parse_metrics(self, f):
    """Parses timestamped metric lines.

    Given metrics lines like:

    2017-10-25 10:08:30.961510 \\
        87d5562a5fe0ea075ebb2efb0300d10d23bfa474645bb464d222976ed872df2a \\
            cpu user 33 system 15
    2017-10-25 10:08:30.961510 HOST memory total_gb 16.000 used_gb 8.500
    2017-10-25 10:08:30.961510 HOST disk total_gb 500.000 used_gb 250.000
    2017-10-25 10:08:30.961510 \\
        87d5562a5fe0ea075ebb2efb0300d10d23bfa474645bb464d222976ed872df2a \\
        disk used_mb 150.50

    Returns an iterable of (ts, container, user_cpu, system_cpu, memory_mb,
    host_memory_gb, container_disk_mb, host_disk_gb, host_cpu_user, host_cpu_system).
    It also updates container.peak_total_rss and container.total_user_cpu and
    container.total_system_cpu.
    """
    prev_by_container = {}
    peak_rss_by_container = {}
    current_memory_by_container = {}
    current_disk_by_container = {}
    host_memory_gb = 0  # Track current host memory usage
    host_disk_gb = 0   # Track current host disk usage
    host_cpu_user = 0  # Track current host CPU user %
    host_cpu_system = 0  # Track current host CPU system %
    for line in f:
      ts, rest = split_timestamp(line.rstrip())
      total_rss = None
      try:
        container, metric_type, rest2 = rest.split(" ", 2)
        if container == "HOST" and metric_type == "memory":
          # Parse host memory: "total_gb 16.000 used_gb 8.500"
          metrics = rest2.split(" ")
          if "used_gb" in metrics:
            host_memory_gb = float(metrics[metrics.index("used_gb") + 1])
          continue
        elif container == "HOST" and metric_type == "disk":
          # Parse host disk: "total_gb 500.000 used_gb 250.000"
          metrics = rest2.split(" ")
          if "used_gb" in metrics:
            host_disk_gb = float(metrics[metrics.index("used_gb") + 1])
          continue
        elif container == "HOST" and metric_type == "cpu":
          # Parse host CPU: "user_pct 15.5 system_pct 5.2 idle_pct 79.3"
          metrics = rest2.split(" ")
          if "user_pct" in metrics:
            host_cpu_user = float(metrics[metrics.index("user_pct") + 1])
          if "system_pct" in metrics:
            host_cpu_system = float(metrics[metrics.index("system_pct") + 1])
          continue
        elif metric_type == "cpu":
          _, user_cpu_s, _, system_cpu_s = rest2.split(" ", 3)
        elif metric_type == "memory":
          memory_metrics = rest2.split(" ")
          # Try to find total_rss (cgroup v1) or anon (cgroup v2) as memory usage
          # indicator
          total_rss = None
          if "total_rss" in memory_metrics:
            total_rss = int(memory_metrics[memory_metrics.index("total_rss") + 1])
          elif "anon" in memory_metrics:
            # In cgroup v2, use anon memory as an approximation of RSS
            total_rss = int(memory_metrics[memory_metrics.index("anon") + 1])
        elif metric_type == "disk":
          # Parse container disk: "used_mb 150.50"
          disk_metrics = rest2.split(" ")
          if "used_mb" in disk_metrics:
            disk_mb = float(disk_metrics[disk_metrics.index("used_mb") + 1])
            current_disk_by_container[container] = disk_mb
          continue
      except:
        logging.warning("Skipping metric line: %s", line)
        continue

      if total_rss is not None:
        peak_rss_by_container[container] = max(peak_rss_by_container.get(container, 0),
            total_rss)
        current_memory_by_container[container] = total_rss
        continue

      prev_ts, prev_user, prev_system = prev_by_container.get(
          container, (None, None, None))
      user_cpu = int(user_cpu_s)
      system_cpu = int(system_cpu_s)
      if prev_ts is not None:
        # Timestamps are seconds since the epoch and are floats.
        dt = ts - prev_ts
        assert isinstance(dt, float)
        if dt != 0:
          # Get current memory usage in MB
          memory_mb = current_memory_by_container.get(container, 0) / (1024 * 1024)
          container_disk_mb = current_disk_by_container.get(container, 0)
          yield (ts, container, (user_cpu - prev_user) / dt / USER_HZ,
                 (system_cpu - prev_system) / dt / USER_HZ, memory_mb, host_memory_gb,
                 container_disk_mb, host_disk_gb, host_cpu_user, host_cpu_system)
      prev_by_container[container] = ts, user_cpu, system_cpu

    # Now update container totals
    for c in self.containers:
      if c.id in prev_by_container:
        _, u, s = prev_by_container[c.id]
        c.total_user_cpu, c.total_system_cpu = u // USER_HZ, s // USER_HZ
      if c.id in peak_rss_by_container:
        c.peak_total_rss = peak_rss_by_container[c.id]

  def create_inline_test_reports(self):
    """Creates inline HTML test reports for containers based on TEST-*.xml files."""
    inline_html_parts = []

    for container in self.containers:
      # Look for test XML files in the container's log directory
      container_log_dir = os.path.dirname(container.logfile)
      test_xml_files = []

      # Search for XML files recursively in the container directory
      for root, dirs, files in os.walk(container_log_dir):
        for file in files:
          if file.endswith('.xml') and 'TEST-' in file:
            test_xml_files.append(os.path.join(root, file))

      if not test_xml_files:
        continue

      # Parse test results from XML files
      total_tests = 0
      total_failures = 0
      total_errors = 0
      total_skipped = 0
      test_cases = []

      for xml_file in test_xml_files:
        try:
          tree = ET.parse(xml_file)
          root = tree.getroot()

          # Parse testsuite element
          if root.tag == 'testsuite':
            testsuite = root
          else:
            testsuite = root.find('testsuite')

          if testsuite is not None:
            # Aggregate counts
            total_tests += int(testsuite.get('tests', 0))
            total_failures += int(testsuite.get('failures', 0))
            total_errors += int(testsuite.get('errors', 0))
            total_skipped += int(testsuite.get('skipped', 0))

            # Collect individual test cases
            for testcase in testsuite.findall('testcase'):
              case_info = {
                'name': testcase.get('name', ''),
                'classname': testcase.get('classname', ''),
                'time': testcase.get('time', '0'),
                'status': 'passed'
              }

              # Check for failure or error
              if testcase.find('failure') is not None:
                case_info['status'] = 'failed'
                failure_elem = testcase.find('failure')
                case_info['failure_message'] = failure_elem.get('message', '')
                case_info['failure_detail'] = failure_elem.text or ''
              elif testcase.find('error') is not None:
                case_info['status'] = 'error'
                error_elem = testcase.find('error')
                case_info['error_message'] = error_elem.get('message', '')
                case_info['error_detail'] = error_elem.text or ''
              elif testcase.get('skipped') or testcase.find('skipped') is not None:
                case_info['status'] = 'skipped'

              test_cases.append(case_info)
        except Exception as e:
          logging.warning("Error parsing test XML file %s: %s", xml_file, e)
          continue

      if total_tests == 0:
        continue

      # Generate inline HTML for this container
      container_html = self._generate_inline_test_report_html(
        container.name, total_tests, total_failures,
        total_errors, total_skipped, test_cases)

      inline_html_parts.append(container_html)
      logging.info("Generated inline test report for container %s (%d tests)",
                   container.name, total_tests)
    if not inline_html_parts:
      return "<p>No test reports available.</p>"

    return "\\n".join(inline_html_parts)

  def _generate_test_report_html(self, container_name, test_name, total_tests,
                                total_failures, total_errors, total_skipped, test_cases):
    """Generate HTML content for a container test report."""

    success_rate = (((total_tests - total_failures - total_errors) / total_tests * 100)
                    if total_tests > 0 else 0)
    passed_count = total_tests - total_failures - total_errors - total_skipped

    html = """<!DOCTYPE html>
<html>
<head>
    <title>Test Report - {container_name}</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; }}
        .header {{ background-color: #f5f5f5; padding: 20px; border-radius: 5px; \
margin-bottom: 20px; }}
        .summary {{ display: flex; gap: 20px; margin-bottom: 20px; }}
        .summary-item {{ padding: 10px; border-radius: 5px; text-align: center; \
min-width: 80px; }}
        .summary-total {{ background-color: #e3f2fd; }}
        .summary-passed {{ background-color: #e8f5e8; }}
        .summary-failed {{ background-color: #ffebee; }}
        .summary-error {{ background-color: #fff3e0; }}
        .summary-skipped {{ background-color: #f3e5f5; }}
        .test-table {{ width: 100%; border-collapse: collapse; }}
        .test-table th, .test-table td {{ border: 1px solid #ddd; padding: 8px; \
text-align: left; }}
        .test-table th {{ background-color: #f2f2f2; }}
        .status-passed {{ color: green; font-weight: bold; }}
        .status-failed {{ color: red; font-weight: bold; }}
        .status-error {{ color: orange; font-weight: bold; }}
        .status-skipped {{ color: gray; font-weight: bold; }}
        .failure-detail {{ margin-top: 5px; padding: 5px; background-color: #fff5f5; \
border-left: 3px solid red; font-family: monospace; font-size: 12px; }}
        .error-detail {{ margin-top: 5px; padding: 5px; background-color: #fff9e6; \
border-left: 3px solid orange; font-family: monospace; font-size: 12px; }}
        .back-link {{ margin-bottom: 20px; }}
        .back-link a {{ color: #1976d2; text-decoration: none; }}
        .back-link a:hover {{ text-decoration: underline; }}
    </style>
</head>
<body>
    <div class="back-link">
        <a href="timeline.html">&larr; Back to Timeline</a>
    </div>

    <div class="header">
        <h1>Test Report: {container_name}</h1>
        <p><strong>Test Suite:</strong> {test_name_upper}</p>
        <p><strong>Success Rate:</strong> {success_rate:.1f}%</p>
    </div>

    <div class="summary">
        <div class="summary-item summary-total">
            <div><strong>{total_tests}</strong></div>
            <div>Total</div>
        </div>
        <div class="summary-item summary-passed">
            <div><strong>{passed_count}</strong></div>
            <div>Passed</div>
        </div>
        <div class="summary-item summary-failed">
            <div><strong>{total_failures}</strong></div>
            <div>Failed</div>
        </div>
        <div class="summary-item summary-error">
            <div><strong>{total_errors}</strong></div>
            <div>Error</div>
        </div>
        <div class="summary-item summary-skipped">
            <div><strong>{total_skipped}</strong></div>
            <div>Skipped</div>
        </div>
    </div>

    <h2>Test Cases</h2>
    <table class="test-table">
        <thead>
            <tr>
                <th>Test Name</th>
                <th>Class</th>
                <th>Status</th>
                <th>Time (s)</th>
                <th>Details</th>
            </tr>
        </thead>
        <tbody>""".format(
        container_name=container_name,
        test_name_upper=test_name.upper(),
        success_rate=success_rate,
        total_tests=total_tests,
        passed_count=passed_count,
        total_failures=total_failures,
        total_errors=total_errors,
        total_skipped=total_skipped
    )

    for case in test_cases:
        status_class = "status-{}".format(case['status'])
        details = ""

        if case['status'] == 'failed':
            details = """<div class="failure-detail">
                <strong>Failure:</strong> {}<br>
                <pre>{}</pre>
            </div>""".format(
                case.get('failure_message', ''),
                case.get('failure_detail', '')
            )
        elif case['status'] == 'error':
            details = """<div class="error-detail">
                <strong>Error:</strong> {}<br>
                <pre>{}</pre>
            </div>""".format(
                case.get('error_message', ''),
                case.get('error_detail', '')
            )

        html += """
            <tr>
                <td>{}</td>
                <td>{}</td>
                <td class="{}">{}</td>
                <td>{}</td>
                <td>{}</td>
            </tr>""".format(
                case['name'],
                case['classname'],
                status_class,
                case['status'].upper(),
                case['time'],
                details
            )

    html += """
        </tbody>
    </table>
</body>
</html>"""

    return html

  def _generate_inline_test_report_html(self, container_name, total_tests,
                                       total_failures, total_errors, total_skipped,
                                       test_cases):
    """Generate inline HTML content for a container test report (for embedding in
    timeline)."""

    success_rate = ((total_tests - total_failures - total_errors) / total_tests * 100) \
        if total_tests > 0 else 0
    passed_count = total_tests - total_failures - total_errors - total_skipped

    # Generate a compact version for inline display
    html = """
    <div style="margin-bottom: 30px; border: 1px solid #ddd; \\
border-radius: 5px; overflow: hidden;">
      <div style="background-color: #f8f9fa; padding: 15px; \\
border-bottom: 1px solid #ddd;">
        <h3 style="margin: 0; color: #333;">Container: {container_name}</h3>
        <p style="margin: 5px 0 0 0; color: #666;">Success Rate: {success_rate:.1f}% \\
            ({passed_count}/{total_tests} passed)</p>
      </div>

      <div style="padding: 15px;">
        <div style="display: flex; gap: 15px; margin-bottom: 15px; flex-wrap: wrap;">
          <div style="padding: 8px 12px; border-radius: 3px; background-color: #e3f2fd; \
text-align: center; min-width: 60px;">
            <strong>{total_tests}</strong><br><small>Total</small>
          </div>
          <div style="padding: 8px 12px; border-radius: 3px; background-color: #e8f5e8; \
text-align: center; min-width: 60px;">
            <strong>{passed_count}</strong><br><small>Passed</small>
          </div>
          <div style="padding: 8px 12px; border-radius: 3px; background-color: #ffebee; \
text-align: center; min-width: 60px;">
            <strong>{total_failures}</strong><br><small>Failed</small>
          </div>
          <div style="padding: 8px 12px; border-radius: 3px; background-color: #fff3e0; \
text-align: center; min-width: 60px;">
            <strong>{total_errors}</strong><br><small>Error</small>
          </div>
          <div style="padding: 8px 12px; border-radius: 3px; background-color: #f3e5f5; \
text-align: center; min-width: 60px;">
            <strong>{total_skipped}</strong><br><small>Skipped</small>
          </div>
        </div>""".format(
        container_name=container_name,
        success_rate=success_rate,
        total_tests=total_tests,
        passed_count=passed_count,
        total_failures=total_failures,
        total_errors=total_errors,
        total_skipped=total_skipped)

    # Add test cases table if there are any failures or errors
    if total_failures > 0 or total_errors > 0:
      html += '''
        <details style="margin-top: 10px;">
          <summary style="cursor: pointer; font-weight: bold; color: #d32f2f;">\
View Failed/Error Tests ({} issues)</summary>
          <div style="margin-top: 10px; max-height: 300px; overflow-y: auto;">
            <table style="width: 100%; border-collapse: collapse; font-size: 12px;">
              <thead>
                <tr style="background-color: #f5f5f5;">
                  <th style="border: 1px solid #ddd; padding: 6px; text-align: left;">\
Test Name</th>
                  <th style="border: 1px solid #ddd; padding: 6px; text-align: left;">\
Status</th>
                  <th style="border: 1px solid #ddd; padding: 6px; text-align: left;">\
Time</th>
                  <th style="border: 1px solid #ddd; padding: 6px; text-align: left;">\
Details</th>
                </tr>
              </thead>
              <tbody>'''.format(total_failures + total_errors)

      for case in test_cases:
        if case['status'] in ['failed', 'error']:
          status_color = '#d32f2f' if case['status'] == 'failed' else '#ff9800'
          failure_detail = case.get('failure_detail', case.get('error_detail', ''))
          detail_text = failure_detail[:100] + '...' if len(failure_detail) > 100 \
              else failure_detail

          html += '''
                <tr>
                  <td style="border: 1px solid #ddd; padding: 6px;">{name}</td>
                  <td style="border: 1px solid #ddd; padding: 6px; color: {color}; \
font-weight: bold;">{status}</td>
                  <td style="border: 1px solid #ddd; padding: 6px;">{time}s</td>
                  <td style="border: 1px solid #ddd; padding: 6px; \
font-family: monospace; font-size: 10px;">{detail}</td>
                </tr>'''.format(
            name=(case['name'][:50] + '...'
                  if len(case['name']) > 50 else case['name']),
            status=case['status'].upper(),
            color=status_color,
            time=case['time'],
            detail=detail_text.replace('<', '&lt;').replace('>', '&gt;'))

      html += '''
              </tbody>
            </table>
          </div>
        </details>'''

    html += '''
      </div>
    </div>'''

    return html

  def create(self, output):
    # Generate per-container test reports as inline HTML (not separate files)
    inline_test_reports = self.create_inline_test_reports()

    # Read logfiles
    timelines = []
    for c in self.containers:
      if not os.path.exists(c.logfile):
        logging.warning("Missing log file: %s", c.logfile)
        continue
      timelines.append(self.logfile_timeline(c))

    # Convert timelines to JSON
    min_ts = None
    timeline_json = []
    for timeline in timelines:
      for current_line, next_line in zip(timeline, timeline[1:]):
        name, ts_current, msg = current_line
        _, ts_next, _ = next_line
        timeline_json.append(
            [name, msg, ts_current, ts_next]
        )
    if not timeline_json:
      logging.warning("No timeline data from logfiles; will try to generate timeline "
                      "from metrics only")
      # Continue with empty timeline data, but still try to process metrics
      timeline_min_ts = float('inf')
    else:
      timeline_min_ts = min(x[2] for x in timeline_json) \
          if timeline_json else float('inf')

    # Find the minimum timestamp from BOTH timeline events AND metrics
    metrics_min_ts = float('inf')
    container_by_id = dict()
    for c in self.containers:
      container_by_id[c.id] = c

    metrics_file_exists = os.path.exists(self.monitor_file)
    if metrics_file_exists:
      try:
        for (ts, container_id, user, system, memory, host_memory, container_disk,
             host_disk, host_cpu_user, host_cpu_system) in (
             self.parse_metrics(open(self.monitor_file))):
          container = container_by_id.get(container_id)
          if container:  # Only consider metrics for containers we're tracking
            metrics_min_ts = min(metrics_min_ts, ts)
      except Exception as e:
        logging.warning("Error parsing metrics file for min timestamp: %s", e)

    # Use the overall minimum timestamp from both sources
    min_ts = min(timeline_min_ts, metrics_min_ts)
    if min_ts == float('inf'):
      min_ts = 0  # Fallback if no data



    for row in timeline_json:
      row[2] = row[2] - min_ts
      row[3] = row[3] - min_ts

    # metrics_by_container: container -> [ ts, user, system, memory, disk ]
    # host_memory_data: list of [ts, memory_gb]
    # host_disk_data: list of [ts, disk_gb]
    # host_cpu_data: list of [ts, user_pct, system_pct]
    metrics_by_container = dict()
    host_memory_data = []
    host_disk_data = []
    host_cpu_data = []
    max_metric_ts = 0

    if metrics_file_exists:
      try:
        for (ts, container_id, user, system, memory, host_memory, container_disk,
             host_disk, host_cpu_user, host_cpu_system) in (
             self.parse_metrics(open(self.monitor_file))):
          container = container_by_id.get(container_id)
          if not container:
            continue
          if ts > max_metric_ts:
            max_metric_ts = ts

          # Add container metrics
          metrics_by_container.setdefault(container.name, []).append(
              (ts - min_ts, user, system, memory, container_disk))

          # Add host memory data (avoid duplicates by checking if timestamp
          # already exists)
          adjusted_ts = ts - min_ts
          if not host_memory_data or host_memory_data[-1][0] != adjusted_ts:
            host_memory_data.append([adjusted_ts, host_memory])

          # Add host disk data (avoid duplicates by checking if timestamp already exists)
          if not host_disk_data or host_disk_data[-1][0] != adjusted_ts:
            host_disk_data.append([adjusted_ts, host_disk])

          # Add host CPU data (avoid duplicates by checking if timestamp already exists)
          if not host_cpu_data or host_cpu_data[-1][0] != adjusted_ts:
            host_cpu_data.append([adjusted_ts, host_cpu_user, host_cpu_system])

      except Exception as e:
        logging.warning("Error parsing metrics file: %s", e)

    with open(output, "w") as o:
      template_path = os.path.join(os.path.dirname(__file__), "timeline.html.template")
      shutil.copyfileobj(open(template_path), o)

      # Test reports are already generated as inline HTML above

      o.write("\n<script>\nvar data = \n")
      try:
        json.dump(dict(buildname=self.buildname, timeline=timeline_json,
            metrics=metrics_by_container, host_memory=host_memory_data,
            host_disk=host_disk_data, host_cpu=host_cpu_data,
            max_ts=(max_metric_ts - min_ts if metrics_file_exists else 0)),
            o, indent=2)
        o.write(";\n\n")
        # Add test report data as a separate JavaScript variable
        o.write("var testReportData = ")
        json.dump(inline_test_reports, o)
        o.write(";\n</script>")
      except Exception as e:
        logging.error("Failed to write timeline JSON data: %s", e)
        # Write a minimal valid JSON to avoid breaking the timeline
        json.dump(dict(buildname=self.buildname, timeline=timeline_json,
                  metrics=metrics_by_container, host_memory=[], host_disk=[],
                  host_cpu=[], max_ts=0), o, indent=2)
        o.write(";\nvar testReportData = ")
        fallback_report = "<p>No test reports available.</p>"
        json.dump(inline_test_reports if 'inline_test_reports' in locals()
                  else fallback_report, o)
        o.write(";\n</script>")
      o.close()
