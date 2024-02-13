package org.apache.impala.catalog.events;

import com.google.common.base.Preconditions;
import com.google.common.util.concurrent.ThreadFactoryBuilder;

import java.util.Map;
import java.util.Queue;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;

import org.apache.impala.catalog.CatalogException;
import org.apache.impala.catalog.events.MetastoreEvents.MetastoreEvent;
import org.apache.impala.catalog.events.MetastoreEvents.MetastoreEventType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public class TableEventExecutor {
  private static final Logger LOG = LoggerFactory.getLogger(TableEventExecutor.class);
  Map<String, TableProcessor> tableProcessors_ = new ConcurrentHashMap<>();
  private ScheduledExecutorService ses_;

  AtomicInteger outstandingEventCount_ = new AtomicInteger(0);

  public int getOutstandingEventCount() {
    return outstandingEventCount_.get();
  }

  public void incrementOutstandingEventCount() {
    outstandingEventCount_.incrementAndGet();
  }

  public void decrementOutstandingEventCount() {
    outstandingEventCount_.decrementAndGet();
  }

  public static class TableProcessor {
    String dbName_;
    String tableName_;
    int removeCount_;
    long lastProcessedEventId_ = -1;
    AtomicLong skipEventId_ = new AtomicLong(-1); // used to skip all the events till this drop table event id
    TableEventExecutor tableEventExecutor_;
    Queue<MetastoreEvent> events_ = new ConcurrentLinkedQueue<>();

    public TableProcessor(TableEventExecutor tableEventExecutor, String dbName, String tableName) {
      tableEventExecutor_ = tableEventExecutor;
      dbName_ = dbName;
      tableName_ = tableName;
    }

    public TableEventExecutor getTableEventExecutor() {
      return tableEventExecutor_;
    }

    public String getTableName() {
      return tableName_;
    }

    public void enqueue(MetastoreEvent event) {
      removeCount_ = 0;
      if (event.getEventType() == MetastoreEventType.DROP_TABLE) {
        skipEventId_.set(event.getEventId());
      }
      events_.offer(event);
      tableEventExecutor_.incrementOutstandingEventCount();
    }

    public boolean canBeRemoved() {
      return events_.isEmpty() && ++removeCount_ ==  10; // TODO: Read config for threshold
    }

    public void process() {
      MetastoreEvent event;
      long skipEventId = skipEventId_.get();
      while ((event = events_.peek()) != null) {
        try {
          // Skip all the prior events when the drop data table event is queued behind
          if (event.getEventId() >= skipEventId) {
            if (event instanceof DBBarrierEvent) {
              if (event.getEventId() != lastProcessedEventId_) {
                // Indicate the processing has reached this event
                ((DBBarrierEvent) event).proceed();
                LOG.info("[Table processor:{}.{}] Event type: {}, id: {} barrier reached", dbName_, tableName_, event.getEventType(), event.getEventId());
                lastProcessedEventId_ = event.getEventId();
              }
            } else {
              event.processIfEnabled();
            }
          } else if (!(event instanceof DBBarrierEvent)) {
            // Increment EVENTS_SKIPPED_METRIC for table event when its processing is skipped
            event.getMetrics().getCounter(MetastoreEventsProcessor.EVENTS_SKIPPED_METRIC).inc();
            LOG.info("[Table processor:{}.{}] Event type: {}, id: {} incremented skipped metric to {}", dbName_, tableName_, event.getEventType(), event.getEventId(),
                event.getMetrics().getCounter(MetastoreEventsProcessor.EVENTS_SKIPPED_METRIC).getCount());
          }
          if (event instanceof DBBarrierEvent) {
            if (event.getEventId() == lastProcessedEventId_) {
              if (!((DBBarrierEvent) event).isProcessed()) {
                // Waiting for the db event to be processed
                LOG.info("[Table processor:{}.{}] Event type: {}, id: {} waiting to be processed on DB processor", dbName_, tableName_, event.getEventType(), event.getEventId());
                return;
              }
            } else {
              ((DBBarrierEvent) event).proceed();
              // Do not increment EVENTS_SKIPPED_METRIC for db barrier event in table processor
              LOG.info("[Table processor:{}.{}] Event type: {}, id: {} barrier skipped", dbName_, tableName_, event.getEventType(), event.getEventId());
            }
          }
          lastProcessedEventId_ = event.getEventId();
          events_.poll();
          LOG.info("[Table processor:{}.{}] Event type: {}, id: {} polled. LastProcessedEventId: {}, SkipEventId: {}", dbName_, tableName_, event.getEventType(), event.getEventId(), lastProcessedEventId_, skipEventId);
          tableEventExecutor_.decrementOutstandingEventCount();
        } catch (CatalogException | MetastoreNotificationException e) {
          // tbd
        }
      }
      skipEventId_.compareAndSet(skipEventId, -1);
    }
  }
  public TableEventExecutor(String executorNamePrefix, String name) {
    String executorName = executorNamePrefix + ":TableEventExecutor:" + name;
    ses_ = Executors.newSingleThreadScheduledExecutor(
        new ThreadFactoryBuilder().setDaemon(true).setNameFormat(executorName).build());
  }

  public void start() {
    Preconditions.checkNotNull(ses_);
    ses_.scheduleAtFixedRate(this::process, 100, 100, TimeUnit.MILLISECONDS);
  }

  public void stop() {
    MetastoreEventsProcessor.shutdownAndAwaitTermination(ses_);
    tableProcessors_.clear();
  }

  public TableProcessor getOrCreateTableProcessor(String dbName, String tableName) {
    String canonicalTableName = dbName + ':' + tableName;
    TableProcessor tableProcessor = tableProcessors_.get(canonicalTableName);
    if (tableProcessor == null) {
      tableProcessor = new TableProcessor(this, dbName, tableName);
      tableProcessors_.put(canonicalTableName, tableProcessor);
    }
    return tableProcessor;
  }

  public void deleteTableProcessor(String dbName, String tableName) {
    tableProcessors_.remove(dbName + ':' + tableName);
  }

  public void process() {
    for (Map.Entry<String, TableProcessor> entry : tableProcessors_.entrySet()) {
      TableProcessor tableProcessor = entry.getValue();
      tableProcessor.process();
    }
  }
}
