"""
kombu.transport.mongodb
=======================

MongoDB transport.

:copyright: (c) 2010 - 2012 by Flavio Percoco Premoli.
:license: BSD, see LICENSE for more details.

"""
from __future__ import absolute_import

import pymongo

from pymongo import errors
from anyjson import loads, dumps
from pymongo.connection import Connection

from kombu.exceptions import StdConnectionError, StdChannelError
from kombu.five import Empty

from . import virtual

DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 27017

__author__ = """\
Flavio [FlaPer87] Percoco Premoli <flaper87@flaper87.org>;\
Scott Lyons <scottalyons@gmail.com>;\
"""


class Channel(virtual.Channel):
    _client = None
    supports_fanout = True
    _fanout_queues = {}

    def __init__(self, *vargs, **kwargs):
        super_ = super(Channel, self)
        super_.__init__(*vargs, **kwargs)

        self._queue_cursors = {}
        self._queue_readcounts = {}

    def _new_queue(self, queue, **kwargs):
        pass

    def _get(self, queue):
        try:
            if queue in self._fanout_queues:
                self._ensure_cursor(queue)
                msg = next(self._queue_cursors[queue])
                self._queue_readcounts[queue] += 1
                return loads(msg['payload'])
            else:
                msg = self.client.command(
                    'findandmodify', 'messages',
                    query={'queue': queue},
                    sort={'_id': pymongo.ASCENDING}, remove=True,
                )
        except errors.OperationFailure as exc:
            if 'No matching object found' in exc.args[0]:
                raise Empty()
            raise
        except StopIteration:
            raise Empty()

        # as of mongo 2.0 empty results won't raise an error
        if msg['value'] is None:
            raise Empty()
        return loads(msg['value']['payload'])

    def _size(self, queue):
        if queue in self._fanout_queues:
            self._ensure_cursor(queue)
            return (self._queue_cursors[queue].count() -
                    self._queue_readcounts[queue])

        return self.client.messages.find({'queue': queue}).count()

    def _put(self, queue, message, **kwargs):
        self.client.messages.insert({'payload': dumps(message),
                                     'queue': queue})

    def _purge(self, queue):
        size = self._size(queue)
        if queue in self._fanout_queues:
            cursor = self._queue_cursors[queue]
            cursor.rewind()
            self._queue_cursors[queue] = cursor.skip(cursor.count())
        else:
            self.client.messages.remove({'queue': queue})
        return size

    def close(self):
        super(Channel, self).close()
        if self._client:
            self._client.connection.end_request()

    def _open(self):
        """
        See mongodb uri documentation:
        http://www.mongodb.org/display/DOCS/Connections
        """
        client = self.connection.client
        hostname = client.hostname or DEFAULT_HOST
        authdb = dbname = client.virtual_host

        if dbname in ["/", None]:
            dbname = "kombu_default"
            authdb = "admin"

        if not client.userid:
            hostname = hostname.replace('/' + client.virtual_host, '/')
        else:
            hostname = hostname.replace('/' + client.virtual_host,
                                        '/' + authdb)

        mongo_uri = 'mongodb://' + hostname
        # At this point we expect the hostname to be something like
        # (considering replica set form too):
        #
        #   mongodb://[username:password@]host1[:port1][,host2[:port2],
        #   ...[,hostN[:portN]]][/[?options]]
        mongoconn = Connection(host=mongo_uri, ssl=client.ssl)
        database = getattr(mongoconn, dbname)

        version = mongoconn.server_info()['version']
        if tuple(map(int, version.split('.')[:2])) < (1, 3):
            raise NotImplementedError(
                'Kombu requires MongoDB version 1.3+ (server is {0})'.format(
                    version))

        self.db = database
        col = database.messages
        col.ensure_index([('queue', 1), ('_id', 1)], background=True)

        if 'messages.broadcast' not in database.collection_names():
            capsize = (client.transport_options.get('capped_queue_size')
                       or 100000)
            database.create_collection('messages.broadcast',
                                       size=capsize, capped=True)

        self.bcast = getattr(database, 'messages.broadcast')
        self.bcast.ensure_index([('queue', 1)])

        self.routing = getattr(database, 'messages.routing')
        self.routing.ensure_index([('queue', 1), ('exchange', 1)])
        return database

    #TODO: Store a more complete exchange metatable in the routing collection
    def get_table(self, exchange):
        """Get table of bindings for ``exchange``."""
        localRoutes = frozenset(self.state.exchanges[exchange]['table'])
        brokerRoutes = self.client.messages.routing.find(
            {'exchange': exchange}
        )

        return localRoutes | frozenset((r['routing_key'],
                                        r['pattern'],
                                        r['queue']) for r in brokerRoutes)

    def _put_fanout(self, exchange, message, **kwargs):
        """Deliver fanout message."""
        self.client.messages.broadcast.insert({'payload': dumps(message),
                                               'queue': exchange})

    def _queue_bind(self, exchange, routing_key, pattern, queue):
        if self.typeof(exchange).type == 'fanout':
            self._fanout_queues[queue] = exchange
            self._ensure_cursor(queue)

        meta = {'exchange': exchange,
                'queue': queue,
                'routing_key': routing_key,
                'pattern': pattern}
        self.client.messages.routing.update(meta, meta, upsert=True)

    def queue_delete(self, queue, **kwargs):
        self.routing.remove({'queue': queue})
        super(Channel, self).queue_delete(queue, **kwargs)
        if queue in self._fanout_queues:
            cursor = self._queue_cursors.pop(queue, None)
            if cursor is not None:
                cursor.close()
            self._fanout_queues.pop(queue, None)

    @property
    def client(self):
        if self._client is None:
            self._client = self._open()
        return self._client

    def _ensure_cursor(self, queue):
        self.client
        exchange = self._fanout_queues[queue]
        if queue not in self._queue_cursors:
            cursor = self.bcast.find(query={'queue': exchange},
                             sort=[('$natural', 1)], tailable=True)
            # Fast forward the cursor past old events
            count = cursor.count()
            self._queue_cursors[queue] = cursor.skip(count)
            self._queue_readcounts[queue] = count

class Transport(virtual.Transport):
    Channel = Channel

    polling_interval = 1
    default_port = DEFAULT_PORT
    connection_errors = (StdConnectionError, errors.ConnectionFailure)
    channel_errors = (StdChannelError,
                      errors.ConnectionFailure,
                      errors.OperationFailure)
    driver_type = 'mongodb'
    driver_name = 'pymongo'

    def driver_version(self):
        return pymongo.version
