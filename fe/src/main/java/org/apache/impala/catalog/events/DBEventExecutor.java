package org.apache.impala.catalog.events;

import com.google.common.base.Preconditions;
import com.google.common.util.concurrent.ThreadFactoryBuilder;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.Queue;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;

import org.apache.impala.catalog.CatalogException;
import org.apache.impala.catalog.events.MetastoreEvents.MetastoreEvent;
import org.apache.impala.catalog.events.MetastoreEvents.MetastoreEventType;
import org.apache.impala.catalog.events.TableEventExecutor.TableProcessor;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public class DBEventExecutor {
  private static final Logger LOG = LoggerFactory.getLogger(DBEventExecutor.class);
  private static Map<String, DBEventExecutor> dbNameToEventExecutor = new ConcurrentHashMap<>();

  private ScheduledExecutorService ses_;
  private Map<String, DBProcessor> dbProcessors_ = new ConcurrentHashMap<>();
  private List<TableEventExecutor> tableEventExecutors_;
  private Map<String, TableEventExecutor> tableToEventExecutor_ = new HashMap<>();

  public static class DBProcessor {
    String dbName_;
    int removeCount_;
    AtomicLong skipEventId_ = new AtomicLong(-1); // skip all the events till this event id
    DBEventExecutor dbEventExecutor_;
    Queue<MetastoreEvent> inEvents_ = new ConcurrentLinkedQueue<>();
    Queue<DBBarrierEvent> dbEvents = new ConcurrentLinkedQueue<>();
    Set<TableProcessor> tableProcessors_ = new HashSet<>();

    public DBProcessor(DBEventExecutor eventExecutor, String dbName) {
      dbEventExecutor_ = eventExecutor;
      dbName_ = dbName;
    }

    public String getDbName() {
      return dbName_;
    }

    public void enqueue(MetastoreEvent event) {
      removeCount_ = 0;
      if (event.getEventType() == MetastoreEventType.DROP_DATABASE) {
        skipEventId_.set(event.getEventId());
      }
      inEvents_.offer(event);
    }

    public boolean canBeRemoved() {
      return inEvents_.isEmpty() && tableProcessors_.isEmpty() && ++removeCount_ ==  10; // TODO: Read config for threshold
    }

    private void dispatchTableEvent(MetastoreEvent event) {
      TableEventExecutor tableEventExecutor = dbEventExecutor_.getOrAssignTableEventExecutor(event.getDbName(), event.getTableName());
      TableProcessor tableProcessor = tableEventExecutor.getOrCreateTableProcessor(event.getDbName(), event.getTableName());
      if (tableProcessor.events_.isEmpty()) {
        /* Table processor is created now. Prepend the pending db events to table processor */
        Iterator<DBBarrierEvent> it = dbEvents.iterator();
        while(it.hasNext()) {
          DBBarrierEvent barrierEvent = it.next();
          barrierEvent.incrExpectedProceedCount();
          LOG.info("[DB processor:{}] Event type: {}, id: {} dispatched to table processor: {}.{}", dbName_, barrierEvent.getEventType(), barrierEvent.getEventId(), dbName_, tableProcessor.tableName_);
          tableProcessor.enqueue(barrierEvent);
        }
      }
      LOG.info("[DB processor:{}] Event type: {}, id: {} dispatched to table processor: {}.{}", dbName_, event.getEventType(), event.getEventId(), dbName_, tableProcessor.tableName_);
      tableProcessor.enqueue(event);
      tableProcessors_.add(tableProcessor);
    }

    private void dispatchDbEvent(MetastoreEvent event) {
      DBBarrierEvent barrierEvent = new DBBarrierEvent(tableProcessors_.size(), event);
      for (TableProcessor tableProcessor : tableProcessors_) {
        LOG.info("[DB processor:{}] Event type: {}, id: {} dispatched to table processor: {}.{}", dbName_, barrierEvent.getEventType(), barrierEvent.getEventId(), dbName_, tableProcessor.tableName_);
        tableProcessor.enqueue(barrierEvent);
      }
      dbEvents.offer(barrierEvent);
    }

    private void processDbEvents(long skipEventId) {
      while (dbEvents.peek() != null) {
        DBBarrierEvent barrierEvent = dbEvents.peek();
        if (barrierEvent.getEventId() < skipEventId) {
          // Skip all the events when the drop database event is queued behind
          barrierEvent.setProcessed(true);
          dbEvents.poll();
          barrierEvent.getMetrics().getCounter(MetastoreEventsProcessor.EVENTS_SKIPPED_METRIC).inc();
          LOG.info("[DB processor:{}] Event type: {}, id: {} incremented skipped metric to {}", dbName_, barrierEvent.getEventType(), barrierEvent.getEventId(),
              barrierEvent.getMetrics().getCounter(MetastoreEventsProcessor.EVENTS_SKIPPED_METRIC).getCount());
        } else {
          if (barrierEvent.expectedProceedCount_.get() == 0) {
            try {
              barrierEvent.processIfEnabled();
              barrierEvent.setProcessed(true);
              dbEvents.poll();
              LOG.info("[DB processor:{}] Event type: {}, id: {} is processed and polled", dbName_, barrierEvent.getEventType(), barrierEvent.getEventId());
            } catch (CatalogException | MetastoreNotificationException e) {
              // tbd
            }
          }
        }
      }
    }

    public void cleanup() {
      Iterator<TableProcessor> it = tableProcessors_.iterator();
      while (it.hasNext()) {
        TableProcessor tableProcessor = it.next();
        if (tableProcessor.canBeRemoved()) {
          tableProcessor.getTableEventExecutor().deleteTableProcessor(getDbName(), tableProcessor.getTableName());
          it.remove();
        }
      }
    }

    public void process() {
      MetastoreEvent event;
      long skipEventId = skipEventId_.get();
      while ((event = inEvents_.peek()) != null) {
        if (isDbEvent(event)) {
          if (event.getEventId() >= skipEventId) {
            dispatchDbEvent(event);
          }
        } else {
          dispatchTableEvent(event);
        }
        inEvents_.poll();
      }
      processDbEvents(skipEventId);
      skipEventId_.compareAndSet(skipEventId, -1);
      cleanup();
    }
  }

  public DBEventExecutor(String name, int tableEventExecutors) {
    String executorName = "DBEventExecutor:" + name;
    ses_ = Executors.newSingleThreadScheduledExecutor(
        new ThreadFactoryBuilder().setDaemon(true).setNameFormat(executorName).build());
    tableEventExecutors_ = new ArrayList<>(tableEventExecutors);
    for (int i = 0; i < tableEventExecutors; i++) {
      tableEventExecutors_.add(new TableEventExecutor(executorName, String.valueOf(i)));
    }
  }

  public void start() {
    Preconditions.checkNotNull(ses_);
    ses_.scheduleAtFixedRate(this::process, 100, 100, TimeUnit.MILLISECONDS);
    tableEventExecutors_.forEach(TableEventExecutor::start);
  }

  public void cleanup() {
    Iterator<Map.Entry<String, DBProcessor>> it = dbProcessors_.entrySet().iterator();
    while(it.hasNext()) {
      DBProcessor dbProcessor = it.next().getValue();
      if (dbProcessor.canBeRemoved()) {
        dbNameToEventExecutor.remove(dbProcessor.dbName_);
        it.remove();
      }
    }
  }

  public void stop() {
    MetastoreEventsProcessor.shutdownAndAwaitTermination(ses_);
    tableEventExecutors_.forEach(TableEventExecutor::stop);
    tableToEventExecutor_.clear();
    dbProcessors_.clear();
  }

  public int getOutstandingEventCount() {
    int count = 0;
    for (TableEventExecutor tee : tableEventExecutors_) {
      count += tee.getOutstandingEventCount();
    }
    return count;
  }

  public void enqueue(MetastoreEvent event) {
    DBProcessor dbProcessor = getOrCreateDBProcessor(event);
    dbProcessor.enqueue(event);
  }

  private DBProcessor getOrCreateDBProcessor(MetastoreEvent event) {
    String dbName = event.getDbName();
    DBProcessor dbProcessor = dbProcessors_.get(dbName);
    if (dbProcessor == null) {
      dbProcessor = new DBProcessor(this, dbName);
      dbProcessors_.put(dbName, dbProcessor);
    }
    return dbProcessor;
  }

  private TableEventExecutor getOrAssignTableEventExecutor(String dbName, String tableName) {
    String canonicalTableName = dbName + ':' + tableName;
    TableEventExecutor eventExecutor = tableToEventExecutor_.get(canonicalTableName);
    if (eventExecutor == null) {
      int minOutStandingEvents = Integer.MAX_VALUE;
      for (TableEventExecutor tee : tableEventExecutors_) {
        if (minOutStandingEvents > tee.getOutstandingEventCount()) {
          minOutStandingEvents = tee.getOutstandingEventCount();
          eventExecutor = tee;
        }
      }
      Preconditions.checkNotNull(eventExecutor);
      tableToEventExecutor_.put(canonicalTableName, eventExecutor);
    }
    return eventExecutor;
  }

  public void process() {
    for (Map.Entry<String, DBProcessor> entry : dbProcessors_.entrySet()) {
      DBProcessor dbProcessor = entry.getValue();
      dbProcessor.process();
    }
  }

  private static boolean isDbEvent(MetastoreEvent event) {
    MetastoreEventType eventType = event.getEventType();
    return eventType == MetastoreEventType.CREATE_DATABASE || eventType == MetastoreEventType.DROP_DATABASE || eventType ==  MetastoreEventType.ALTER_DATABASE;
  }

   public static DBEventExecutor getEventExecutor(String dbName) {
    return dbNameToEventExecutor.get(dbName);
   }

  public static void clearEventExecutors() {
    dbNameToEventExecutor.clear();
  }

  public static void assignEventExecutor(String dbName, DBEventExecutor eventExecutor) {
    Preconditions.checkNotNull(eventExecutor);
    Preconditions.checkState(dbNameToEventExecutor.get(dbName) == null);
    dbNameToEventExecutor.put(dbName, eventExecutor);
  }
}
