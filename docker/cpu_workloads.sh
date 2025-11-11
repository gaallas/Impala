#!/bin/bash

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

# CPU workload functions for Docker container testing
# These functions generate intensive CPU load for timeline visualization

# Generate intensive CPU load for build operations
# Creates system-intensive workload with process management overhead
# Args: $1 - duration in seconds
cpu_workload_build() {
    local duration=${1:-8}
    echo "High CPU and memory load for ${duration} seconds - intensive computation"

    bash -c "
      end=\$((SECONDS + ${duration}))
      # Launch background processes that run continuously
      for proc in {1..8}; do
        (
          # Allocate memory arrays to increase memory usage
          declare -a mem_array
          while [[ \$SECONDS -lt \$end ]]; do
            # Continuous intensive computation with memory allocation
            for i in {1..10000}; do
              [[ \$SECONDS -ge \$end ]] && break 2
              result=\$((i * i * i * i % 999983))
              sum=\$((result + i * 73 + result * i % 997))
              temp=\$((sum * sum % 1000003))
              final=\$((temp + result * i / (i + 1)))
              # Store results in memory to increase memory usage
              mem_array[\$((i % 1000))]=\$final
            done
          done
        ) &
      done
      wait
    "
}

# Generate intensive CPU load for test operations
# Creates user-space intensive workload with mathematical computations
# Args: $1 - duration in seconds
cpu_workload_test() {
    local duration=${1:-12}
    echo "High CPU and memory load for ${duration} seconds - intensive parallel" \
         "computation"

    bash -c "
      end=\$((SECONDS + ${duration}))
      # Launch background processes that run continuously
      for proc in {1..12}; do
        (
          # Allocate larger memory arrays for NOOP (test workload)
          declare -a big_mem_array
          while [[ \$SECONDS -lt \$end ]]; do
            # Continuous intensive user-space computation with memory allocation
            for i in {1..15000}; do
              [[ \$SECONDS -ge \$end ]] && break 2
              a=\$((i * 19 + 11))
              b=\$((a * a * a % 1000003))
              c=\$((b * i * i % 999991))
              d=\$((c + a * b % 997))
              result=\$((d * d * d % 999983))
              # Additional pure computation
              sum=\$((result * i / (i + 1) + a * c))
              # Store more data in memory to increase memory usage significantly
              big_mem_array[\$((i % 2000))]=\$sum
              big_mem_array[\$((i % 2000 + 2000))]=\$result
            done
          done
        ) &
      done
      wait
    "
}

# Lightweight CPU load for minimal testing (original behavior)
# Args: $1 - duration in seconds
cpu_workload_light() {
    local duration=${1:-5}
    echo "Light CPU load for ${duration} seconds"

    bash -c "
      end=\$((SECONDS + ${duration}))
      while [[ \$SECONDS -lt \$end ]]; do
        echo -n >/dev/null
      done
    "
}