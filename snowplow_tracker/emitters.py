"""
    emitters.py

    Copyright (c) 2013-2014 Snowplow Analytics Ltd. All rights reserved.

    This program is licensed to you under the Apache License Version 2.0,
    and you may not use this file except in compliance with the Apache License
    Version 2.0. You may obtain a copy of the Apache License Version 2.0 at
    http://www.apache.org/licenses/LICENSE-2.0.

    Unless required by applicable law or agreed to in writing,
    software distributed under the Apache License Version 2.0 is distributed on
    an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
    express or implied. See the Apache License Version 2.0 for the specific
    language governing permissions and limitations there under.

    Authors: Anuj More, Alex Dean, Fred Blundun
    Copyright: Copyright (c) 2013-2014 Snowplow Analytics Ltd
    License: Apache License Version 2.0
"""

import requests
import json
import threading
import celery
from celery import Celery
from celery.contrib.methods import task
import redis
import logging
from contracts import contract, new_contract
try:
    # Python 2
    from Queue import Queue
except ImportError:
    # Python 3
    from queue import Queue

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

DEFAULT_MAX_LENGTH = 10
PAYLOAD_DATA_SCHEMA = "iglu:com.snowplowanalytics.snowplow/payload_data/jsonschema/1-0-2"

new_contract("protocol", lambda x: x == "http" or x == "https")

new_contract("method", lambda x: x == "get" or x == "post")

new_contract("function", lambda x: hasattr(x, "__call__"))

new_contract("redis", lambda x: isinstance(x, (redis.Redis, redis.StrictRedis)))

try:
    # Check whether a custom Celery configuration module named "snowplow_celery_config" exists
    import snowplow_celery_config
    app = Celery()
    app.config_from_object(snowplow_celery_config)

except ImportError:
    # Otherwise configure Celery with default settings
    app = Celery("Snowplow", broker="redis://guest@localhost//")

class Emitter(object):
    """
        Synchronously send Snowplow events to a Snowplow collector
        Supports both GET and POST requests
    """

    @contract
    def __init__(self, endpoint, protocol="http", port=None, method="get", buffer_size=None, on_success=None, on_failure=None):
        """
            :param endpoint:    The collector URL. Don't include "http://" - this is done automatically.
            :type  endpoint:    string
            :param protocol:    The protocol to use - http or https. Defaults to http.
            :type  protocol:    protocol
            :param port:        The collector port to connect to
            :type  port:        int | None
            :param method:      The HTTP request method
            :type  method:      method
            :param buffer_size: The maximum number of queued events before the buffer is flushed. Default is 10.
            :type  buffer_size: int | None
            :param on_success:  Callback executed after every HTTP request in a flush has status code 200
                                Gets passed the number of events flushed.
            :type  on_success:  function | None
            :param on_failure:  Callback executed if at least one HTTP request in a flush has status code 200
                                Gets passed two arguments:
                                1) The number of events which were successfully sent
                                2) If method is "post": The unsent data in string form;
                                   If method is "get":  An array of dictionaries corresponding to the unsent events' payloads
            :type  on_failure:  function | None            
        """
        self.endpoint = Emitter.as_collector_uri(endpoint, protocol, port, method)

        self.method = method

        if buffer_size is None:
            if method == "post":
                buffer_size = DEFAULT_MAX_LENGTH
            else:
                buffer_size = 1
        self.buffer_size = buffer_size
        self.buffer = []

        self.on_success = on_success
        self.on_failure = on_failure

        self.lock = threading.RLock()

        logger.info("Emitter initialized with endpoint " + self.endpoint)

    @staticmethod
    @contract
    def as_collector_uri(endpoint, protocol="http", port=None, method="get"):
        """
            :param endpoint:  The raw endpoint provided by the user
            :type  endpoint:  string
            :param protocol:  The protocol to use - http or https
            :type  protocol:  protocol            
            :param port:      The collector port to connect to
            :type  port:      int | None            
            :rtype:           string
        """
        if method == "get":
            path = "/i"
        else:
            path = "/com.snowplowanalytics.snowplow/tp2"
        if port is None:
            return protocol + "://" + endpoint + path
        else:
            return protocol + "://" + endpoint + ":" + str(port) + path

    @contract
    def input(self, payload):
        """
            Adds an event to the buffer.
            If the maximum size has been reached, flushes the buffer.

            :param payload:   The name-value pairs for the event
            :type  payload:   dict(string:*)
        """
        with self.lock:
            if self.method == "post":
                items = {key: payload[key] for key in payload}
                for k in items.keys():
                    if not isinstance(items[k], unicode):
                        items[k] = str(items[k])

                self.buffer.append(items)
            else:
                self.buffer.append(payload)

            if len(self.buffer) >= self.buffer_size:
                self.flush()

    @task(name="Flush")
    def flush(self):
        """
            Sends all events in the buffer to the collector.
        """
        with self.lock:
            self.send_events(self.buffer)
            self.buffer = []

    @contract
    def http_post(self, data):
        """
            :param data:  The array of JSONs to be sent
            :type  data:  string
        """
        logger.info("Sending POST request to %s..." % self.endpoint)
        logger.debug("Payload: %s" % data)
        r = requests.post(self.endpoint, data=data, headers={'content-type': 'application/json; charset=utf-8'})
        getattr(logger, "info" if self.is_good_status_code(r.status_code) else "warn")("POST request finished with status code: " + str(r.status_code))
        return r

    @contract
    def http_get(self, payload):
        """
            :param payload:  The event properties
            :type  payload:  dict(string:*)
        """
        logger.info("Sending GET request to %s..." % self.endpoint)
        logger.debug("Payload: %s" % payload)
        r = requests.get(self.endpoint, params=payload)        
        getattr(logger, "info" if self.is_good_status_code(r.status_code) else "warn")("GET request finished with status code: " + str(r.status_code))
        return r

    def sync_flush(self):
        """
            Calls the flush method of the base Emitter class.
            This is guaranteed to be blocking, not asynchronous.
        """
        logger.debug("Starting synchronous flush...")
        result = Emitter.flush(self)
        logger.info("Finished synchrous flush")

    @staticmethod
    @contract
    def is_good_status_code(status_code):
        """
            :param status_code:  HTTP status code
            :type  status_code:  int
            :rtype:              bool
        """
        return 200 <= status_code < 400

    @contract
    def send_events(self, evts):
        """
            :param evts: Array of events to be sent
            :type  evts: list(dict(string:*))
        """
        if len(evts) > 0:
            logger.info("Attempting to send %s requests" % len(evts))
            if self.method == 'post':
                data = json.dumps({
                    "schema": PAYLOAD_DATA_SCHEMA,
                    "data": evts
                }, separators=(',', ':'))
                post_succeeded = False
                try:
                    status_code = self.http_post(data).status_code
                    post_succeeded = self.is_good_status_code(status_code)
                except requests.RequestException as e:
                    logger.warn(e)
                if post_succeeded:
                    if self.on_success is not None:
                        self.on_success(len(evts))
                elif self.on_failure is not None:
                    self.on_failure(0, evts)

            elif self.method == 'get':
                success_count = 0
                unsent_requests = []
                for evt in evts:
                    get_succeeded = False
                    try:
                        status_code = self.http_get(evt).status_code
                        get_succeeded = self.is_good_status_code(status_code)
                    except requests.RequestException as e:
                        logger.warn(e)
                    if get_succeeded:
                        success_count += 1
                    else:
                        unsent_requests.append(evt)
                if len(unsent_requests) == 0:
                    if self.on_success is not None:
                        self.on_success(success_count)
                elif self.on_failure is not None:
                    self.on_failure(success_count, unsent_requests)
        else:
            logger.info("Skipping flush since buffer is empty")

class AsyncEmitter(Emitter):
    """
        Uses threads to send HTTP requests asynchronously
    """

    @contract
    def __init__(
        self,
        endpoint,
        protocol="http",
        port=None,
        method="get",
        buffer_size=None,
        on_success=None,
        on_failure=None,
        thread_count=1):
        """
            :param endpoint:    The collector URL. Don't include "http://" - this is done automatically.
            :type  endpoint:    string
            :param protocol:    The protocol to use - http or https. Defaults to http.
            :type  protocol:    protocol
            :param port:        The collector port to connect to
            :type  port:        int | None
            :param method:      The HTTP request method
            :type  method:      method
            :param buffer_size: The maximum number of queued events before the buffer is flushed. Default is 10.
            :type  buffer_size: int | None
            :param on_success:  Callback executed after every HTTP request in a flush has status code 200
                                Gets passed the number of events flushed.
            :type  on_success:  function | None
            :param on_failure:  Callback executed if at least one HTTP request in a flush has status code 200
                                Gets passed two arguments:
                                1) The number of events which were successfully sent
                                2) If method is "post": The unsent data in string form;
                                   If method is "get":  An array of dictionaries corresponding to the unsent events' payloads
            :type  on_failure:  function | None
            :param thread_count: Number of worker threads to use for HTTP requests
            :type  thread_count: int
        """
        super(AsyncEmitter, self).__init__(endpoint, protocol, port, method, buffer_size, on_success, on_failure)
        self.queue = Queue()
        for i in range(thread_count):
            t = threading.Thread(target=self.consume)
            t.daemon = True
            t.start()

    def sync_flush(self):
        while True:
            self.flush()
            self.queue.join()
            if len(self.buffer) < 1:
                break

    def flush(self):
        """
            Removes all dead threads, then creates a new thread which
            excecutes the flush method of the base Emitter class
        """
        with self.lock:
            self.queue.put(self.buffer)
            self.buffer = []

    def consume(self):
        while True:
            evts = self.queue.get()
            self.send_events(evts)
            self.queue.task_done()


class CeleryEmitter(Emitter):
    """
        Uses a Celery worker to send HTTP requests asynchronously.
        Works like the base Emitter class,
        but on_success and on_failure callbacks cannot be set.
    """
    def __init__(self, endpoint, protocol="http", port=None, method="get", buffer_size=None):
        super(CeleryEmitter, self).__init__(endpoint, protocol, port, method, buffer_size, None, None)

    def flush(self):
        """
            Schedules a flush task
        """
        super(CeleryEmitter, self).flush.delay()
        logger.info("Scheduled a Celery task to flush the event queue")


class RedisEmitter(object):
    """
        Sends Snowplow events to a Redis database
    """
    @contract
    def __init__(self, rdb=None, key="snowplow"):
        """
            :param rdb:  Optional custom Redis database
            :type  rdb:  redis | None
            :param key:  The Redis key for the list of events
            :type  key:  string
        """
        if rdb is None:
            rdb = redis.StrictRedis()
        self.rdb = rdb
        self.key = key

    @contract
    def input(self, payload):
        """
            :param payload:  The event properties
            :type  payload:  dict(string:*)
        """
        logger.debug("Pushing event to Redis queue...")
        self.rdb.rpush(self.key, json.dumps(payload))
        logger.info("Finished sending event to Redis.")

    def flush(self):
        logger.warn("The RedisEmitter class does not need to be flushed")

    def sync_flush(self):
        self.flush()
