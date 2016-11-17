"""!
@brief Classes related to event queue handling.
"""


import collections
import os

import eva.base.adapter
import eva.event
import eva.exceptions
import eva.globe
import eva.job
import eva.zk


class EventQueueItem(object):
    """!
    @brief Wrapper around eva.event.Event, maintaining a list of jobs for a
    specific event.

    This class is a wrapper around the eva.event.Event object, maintaining an
    ordered list of eva.job.Job objects. The class can be iterated to retrieve
    a list of the job objects.
    """

    def __init__(self, event):
        """!
        @brief Initialize a EventQueueItem based on an eva.event.Event object.
        @param event eva.event.Event
        """
        assert isinstance(event, eva.event.Event)

        ## An ordered dictionary of eva.job.Job objects associated with this event.
        self.jobs = collections.OrderedDict()
        ## The eva.event.Event object assigned during class construction.
        self.event = event

    def id(self):
        """!
        @brief Return the event ID.
        """
        return self.event.id()

    def add_job(self, job):
        """!
        @brief Add a eva.job.Job object to this event queue item.
        @param job eva.job.Job
        """
        assert isinstance(job, eva.job.Job)
        self.jobs[job.id] = job

    def remove_job(self, job_id):
        """!
        @brief Remove a eva.job.Job object from this event queue item.
        @param job_id str
        """
        del self.jobs[job_id]

    def empty(self):
        """!
        @brief Returns True if there are no eva.job.Job objects connected to
        this event queue item.
        """
        return len(self.jobs) == 0

    def job_keys(self):
        """!
        @brief Returns an ordered list of job ID's.
        """
        return list(self.jobs.keys())

    def failed_jobs(self):
        """!
        @brief Returns a list of jobs that have failed.
        """
        return [job for key, job in self.jobs.items() if job.failed()]

    def finished(self):
        """!
        @brief Returns True if all jobs have finished, False otherwise.
        @throws RuntimeError Thrown if this function is called on an event
        queue item with zero associated jobs.

        This function must only be called if this EventQueueItem object
        contains jobs, otherwise an exception will be thrown.
        """
        if len(self.jobs) == 0:
            raise RuntimeError('finished() should not be called on an EventQueueItem with zero jobs')
        for job in self:
            if not job.finished():
                return False
        return True

    def serialize(self):
        """!
        @brief Returns a serialized representation of this event queue item,
        suitable for storage in ZooKeeper.
        """
        serialized = {}
        serialized['message'] = self.event.message
        serialized['job_keys'] = self.job_keys()
        serialized['jobs'] = {}
        for key, job in self.jobs.items():
            serialized['jobs'][key] = {
                'status': job.status,
                'adapter': job.adapter.config_id,
            }
        return serialized

    def __iter__(self):
        """!
        @brief Returns an iterator to this event queue item's eva.job.Job
        objects.
        """
        return iter(self.jobs.values())

    def __len__(self):
        """!
        @brief Returns the total number of jobs.
        """
        return len(self.jobs)

    def __repr__(self):
        """!
        @brief Returns a string representation of this object.
        """
        return '<EventQueueItem: event.id=%s>' % self.id()


class EventQueue(eva.globe.GlobalMixin):
    """!
    @brief Manages events under processing, in a queue mirrored to ZooKeeper.

    ZooKeeper paths:

    * `/events`

      A list of event IDs referring to individual ZooKeeper paths. This list
      exists only in order to detect which events are in the processing list.

    * `/events/<EVENT_UUID>`

      A list of jobs IDs, referring to jobs that have been initialized from
      this particular event. This list exists only in order to detect which
      jobs are in the processing list.

    * `/events/<EVENT_UUID>/message`

      The raw message string for an event, as received on the wire. Used for
      reconstructing event objects in case of a program crash or adapter
      failure.

    * `/events/<EVENT_UUID>/<JOB_UUID>/status`

      The job status of this job.

    * `/events/<EVENT_UUID>/<JOB_UUID>/adapter`

      The adapter that owns this job.
    """

    def init(self):
        """!
        @brief Instantiate the event queue.
        """
        ## An ordered dictionary of EventQueueItem objects.
        self.items = collections.OrderedDict()
        ## Base ZooKeeper path of the event queue.
        self.zk_base_path = os.path.join(self.zookeeper.EVA_BASE_PATH, 'events')
        self.zookeeper.ensure_path(self.zk_base_path)

    def add_event(self, event):
        """!
        @brief Add an event to the event queue.
        @param event eva.event.Event
        @throws eva.exceptions.DuplicateEventException Thrown when the event already exists in the event queue.
        @throws eva.exceptions.ZooKeeperDataTooLargeException Thrown when the payload is too large for ZooKeeper storage.
        @throws kazoo.exceptions.ZooKeeperError Generic ZooKeeper error, usually connection related.

        The event will be stored in ZooKeeper as soon as it is added.
        """
        assert isinstance(event, eva.event.Event)

        id = event.id()
        if id in self.items:
            raise eva.exceptions.DuplicateEventException('Event %s already exists in the event queue.', id)
        item = EventQueueItem(event)
        self.items[id] = item
        self.store_item(item)
        self.logger.debug('Event added to event queue: %s', event)
        return item
        #try:
        #except eva.exceptions.ZooKeeperDataTooLargeException as e:
            #self.logger.warning(str(e))
            #return False
        #except kazoo.exceptions.ZooKeeperError as e:
            #self.logger.warning(str(e))
            #return False
        return item

    def adapter_active_job_count(self, adapter):
        """!
        @brief Returns the number of active jobs for a specific adapter.
        @param adapter eva.base.adapter.BaseAdapter
        """
        assert isinstance(event, eva.base.adapter.BaseAdapter)

        active = 0
        for item in self:
            for job in item:
                if job.adapter != adapter:
                    continue
                if not job.started():
                    continue
                active += 1
        return active

    def status_count(self):
        """!
        @brief Return a dictionary with status codes and the total number of
        jobs in the event queue having that specific status.
        @return dict
        """
        status_map = dict(zip(eva.job.ALL_STATUSES, [0] * len(eva.job.ALL_STATUSES)))
        for item in self:
            for job in item:
                status_map[job.status] += 1
        return status_map

    def remove_item(self, item):
        """!
        @brief Remove an EventQueueItem from the event queue.
        @param item eva.eventqueue.EventQueueItem
        @throws kazoo.exceptions.ZooKeeperError
        """
        assert isinstance(item, EventQueueItem)
        id = item.id()
        assert id in self.items
        text = 'Event removed from event queue: %s' % item.event
        del self.items[id]
        self.delete_stored_item(id)
        self.logger.debug(text)

    def item_keys(self):
        """!
        @brief Return the list of eva.event.Event ID's currently in the event
        queue. The list is guaranteed to have the same order as they were
        added.
        """
        return list(self.items.keys())

    def store_list(self):
        """!
        @brief Store the list of event ID's in ZooKeeper.
        @throws eva.exceptions.ZooKeeperDataTooLargeException Thrown when the payload is too large for ZooKeeper storage.
        @throws kazoo.exceptions.ZooKeeperError Generic ZooKeeper error, usually connection related.

        EVA stores events in ZooKeeper under separate paths. ZooKeeper can not
        provide a list of paths to the Kazoo client. Thus, a list of paths is
        necessary for proper processing. This method ensures that this list is
        stored.
        """
        self.store_serialized_data(self.zk_base_path, self.item_keys(), metric_base='event_queue')

    def store_item(self, item):
        """!
        @brief Store a specific EventQueueItem in ZooKeeper. The EventQueueItem must already exist in the event queue.
        @param item eva.eventqueue.EventQueueItem
        @throws eva.exceptions.ZooKeeperDataTooLargeException Thrown when the payload is too large for ZooKeeper storage.
        @throws kazoo.exceptions.ZooKeeperError Generic ZooKeeper error, usually connection related.

        The following information is stored:

          * The initial Event message, useful for reconstructing the
            eva.event.Event object.
          * The list of job ID's connected to the event.
          * An entry for each job that has been generated, containing the
            adapter that generated the job, as well as the job's status code.
        """

        assert isinstance(item, EventQueueItem)
        assert item.id() in self.items

        base_path = os.path.join(self.zk_base_path, item.id())
        self.zookeeper.ensure_path(base_path)
        serialized = item.serialize()
        self.store_serialized_data(os.path.join(base_path, 'message'), serialized['message'])
        self.store_serialized_data(os.path.join(base_path, 'jobs'), serialized['job_keys'])
        for key, job in serialized['jobs'].items():
            path = os.path.join(base_path, 'jobs', key)
            self.zookeeper.ensure_path(path)
            self.store_serialized_data(os.path.join(path, 'adapter'), job['adapter'])
            self.store_serialized_data(os.path.join(path, 'status'), job['status'])
        self.store_list()

    def delete_stored_item(self, item_id):
        """!
        @brief Delete a EventQueueItem from ZooKeeper. The item must exist in
        the event queue.
        @param item_id str The Event ID.
        @throws kazoo.exceptions.ZooKeeperError Generic ZooKeeper error, usually connection related.
        """
        assert isinstance(item_id, str)
        assert item_id in self.items

        path = os.path.join(self.zk_base_path, item_id)
        self.zookeeper.delete(path, recursive=True)
        self.logger.debug('Recursively deleted ZooKeeper path: %s', path)
        self.store_list()

    def store_serialized_data(self, path, data, metric_base=None):
        """!
        @brief Store structured data in ZooKeeper.
        @throws kazoo.exceptions.ZooKeeperError Generic ZooKeeper error, usually connection related.

        In addition to ZooKeeper storage, this function will log the event to
        the debug log, as well as report metrics for the stored item if
        metric_base is not None.
        """
        count, size = eva.zk.store_serialized_data(self.zookeeper, path, data)
        self.logger.debug('Stored %d items of total %d bytes at ZooKeeper path %s', count, size, path)
        if not metric_base:
            return
        self.statsd.gauge('eva_' + metric_base + '_count', count)
        self.statsd.gauge('eva_' + metric_base + '_size', size)

    def empty(self):
        """!
        @brief Returns True if the event queue list is empty.
        """
        return len(self.items) == 0

    def __iter__(self):
        return iter(self.items.values())

    def __len__(self):
        return len(self.items)