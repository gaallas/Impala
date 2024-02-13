package org.apache.impala.catalog.events;

import org.apache.impala.catalog.CatalogException;

import java.util.concurrent.atomic.AtomicInteger;

public class DBBarrierEvent extends MetastoreEvents.MetastoreDatabaseEvent {
  volatile boolean isProcessed_;
  MetastoreEvents.MetastoreEvent event_;
  AtomicInteger expectedProceedCount_;

  public DBBarrierEvent(int tableCount, MetastoreEvents.MetastoreEvent event) {
    super(event.catalogOpExecutor_, event.metrics_, event.event_);
    event_ = event;
    expectedProceedCount_ = new AtomicInteger(tableCount);
  }

  @Override
  protected void process() throws MetastoreNotificationException, CatalogException {
    event_.process();
  }

  @Override
  protected SelfEventContext getSelfEventContext() {
    return event_.getSelfEventContext();
  }

  public void proceed() {
    expectedProceedCount_.decrementAndGet();
  }

  public boolean isProcessed() {
    return isProcessed_;
  }

  public void setProcessed(boolean processed) {
    isProcessed_ = processed;
  }

  public void incrExpectedProceedCount() {
    expectedProceedCount_.incrementAndGet();
  }
}
