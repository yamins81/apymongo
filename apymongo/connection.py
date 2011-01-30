# Copyright 2009-2010 10gen, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you
# may not use this file except in compliance with the License.  You
# may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.  See the License for the specific language governing
# permissions and limitations under the License.

"""Tools for connecting to MongoDB.

.. seealso:: Module :mod:`~pymongo.master_slave_connection` for
   connecting to master-slave clusters, and
   :doc:`/examples/replica_set` for an example of how to connect to a
   replica set.

To get a :class:`~pymongo.database.Database` instance from a
:class:`Connection` use either dictionary-style or attribute-style
access:

.. doctest::

  >>> from pymongo import Connection
  >>> c = Connection()
  >>> c.test_database
  Database(Connection('localhost', 27017), u'test_database')
  >>> c['test-database']
  Database(Connection('localhost', 27017), u'test-database')
"""

import datetime
import os
import select
import struct
import socket
import threading
import time
import warnings
import functools

import tornado.iostream

from apymongo import (database,
                     helpers,
                     message)
from apymongo.cursor_manager import CursorManager
from apymongo.errors import (AutoReconnect,
                            ConfigurationError,
                            ConnectionFailure,
                            DuplicateKeyError,
                            InvalidURI,
                            OperationFailure)


_CONNECT_TIMEOUT = 20.0


def _partition(source, sub):
    """Our own string partitioning method.

    Splits `source` on `sub`.
    """
    i = source.find(sub)
    if i == -1:
        return (source, None)
    return (source[:i], source[i + len(sub):])


def _str_to_node(string, default_port=27017):
    """Convert a string to a node tuple.

    "localhost:27017" -> ("localhost", 27017)
    """
    (host, port) = _partition(string, ":")
    if port:
        port = int(port)
    else:
        port = default_port
    return (host, port)


def _parse_uri(uri, default_port=27017):
    """MongoDB URI parser.
    """

    if uri.startswith("mongodb://"):
        uri = uri[len("mongodb://"):]
    elif "://" in uri:
        raise InvalidURI("Invalid uri scheme: %s" % _partition(uri, "://")[0])

    (hosts, namespace) = _partition(uri, "/")

    raw_options = None
    if namespace:
        (namespace, raw_options) = _partition(namespace, "?")
        if namespace.find(".") < 0:
            db = namespace
            collection = None
        else:
            (db, collection) = namespace.split(".", 1)
    else:
        db = None
        collection = None

    username = None
    password = None
    if "@" in hosts:
        (auth, hosts) = _partition(hosts, "@")

        if ":" not in auth:
            raise InvalidURI("auth must be specified as "
                             "'username:password@'")
        (username, password) = _partition(auth, ":")

    host_list = []
    for host in hosts.split(","):
        if not host:
            raise InvalidURI("empty host (or extra comma in host list)")
        host_list.append(_str_to_node(host, default_port))

    options = {}
    if raw_options:
        and_idx = raw_options.find("&")
        semi_idx = raw_options.find(";")
        if and_idx >= 0 and semi_idx >= 0:
            raise InvalidURI("Cannot mix & and ; for option separators.")
        elif and_idx >= 0:
            options = dict([kv.split("=") for kv in raw_options.split("&")])
        elif semi_idx >= 0:
            options = dict([kv.split("=") for kv in raw_options.split(";")])
        elif raw_options.find("="):
            options = dict([raw_options.split("=")])


    return (host_list, db, username, password, collection, options)

             
def receive_body_on_stream(operation,request_id,strm,callback,header):
                            
        length = struct.unpack("<i", header[:4])[0]
        assert request_id == struct.unpack("<i", header[8:12])[0], \
            "ids don't match %r %r" % (request_id,
                                       struct.unpack("<i", header[8:12])[0])
        assert operation == struct.unpack("<i", header[12:])[0]

        strm.read_bytes(length-16,callback)
        

class _Pool(threading.local):
    """A simple connection pool.

    Uses thread-local stream per thread. By calling return_stream() a
    thread can return a stream to the pool. Right now the pool size is
    capped at 10 streams - we can expose this as a parameter later, if
    needed.
    """

    # Non thread-locals
    __slots__ = ["streams", "stream_factory", "pool_size", "pid"]

    # thread-local default
    stream = None

    def __init__(self, stream_factory):
        self.pid = os.getpid()
        self.pool_size = 10
        self.stream_factory = stream_factory
        if not hasattr(self, "streams"):
            self.streams = []


    def get_stream(self,callback):
        # We use the pid here to avoid issues with fork / multiprocessing.
        # See test.test_connection:TestConnection.test_fork for an example of
        # what could go wrong otherwise
        pid = os.getpid()

        if pid != self.pid:
            self.sock = None
            self.streams = []
            self.pid = pid

        if self.stream is not None and self.stream[0] == pid:
            callback(self.stream[1])

        try:
            self.stream = (pid, self.streams.pop())
        except IndexError:
        
            def stream_callback(strm):
                self.stream = (pid,strm)
                callback(strm)
                
            self.stream_factory(stream_callback)
            
        else:
            callback(self.stream[1])
            

    def return_stream(self):
        if self.stream is not None and self.stream[0] == os.getpid():
            # There's a race condition here, but we deliberately
            # ignore it.  It means that if the pool_size is 10 we
            # might actually keep slightly more than that.
            if len(self.streams) < self.pool_size:
                self.streams.append(self.stream[1])
            else:
                self.stream[1].close()
        self.stream = None


class Connection(object):  # TODO support auth for pooling
    """Connection to MongoDB.
    """

    HOST = "localhost"
    PORT = 27017

    def __init__(self, host=None, port=None, pool_size=None,io_loop=None,
                 auto_start_request=None, timeout=None, slave_okay=False,
                 network_timeout=None, document_class=dict, tz_aware=False,
                 _connect=True):
        """Create a new connection to a single MongoDB instance at *host:port*.

        The resultant connection object has connection-pooling built
        in. It also performs auto-reconnection when necessary. If an
        operation fails because of a connection error,
        :class:`~pymongo.errors.ConnectionFailure` is raised. If
        auto-reconnection will be performed,
        :class:`~pymongo.errors.AutoReconnect` will be
        raised. Application code should handle this exception
        (recognizing that the operation failed) and then continue to
        execute.

        Raises :class:`TypeError` if port is not an instance of
        ``int``. Raises :class:`~pymongo.errors.ConnectionFailure` if
        the connection cannot be made.

        The `host` parameter can be a full `mongodb URI
        <http://dochub.mongodb.org/core/connections>`_, in addition to
        a simple hostname. It can also be a list of hostnames or
        URIs. Any port specified in the host string(s) will override
        the `port` parameter. If multiple mongodb URIs containing
        database or auth information are passed, the last database,
        username, and password present will be used.

        :Parameters:
          - `host` (optional): hostname or IPv4 address of the
            instance to connect to, or a mongodb URI, or a list of
            hostnames / mongodb URIs
          - `port` (optional): port number on which to connect
          - `pool_size` (optional): DEPRECATED
          - `auto_start_request` (optional): DEPRECATED
          - `slave_okay` (optional): is it okay to connect directly to
            and perform queries on a slave instance
          - `timeout` (optional): DEPRECATED
          - `network_timeout` (optional): timeout (in seconds) to use
            for socket operations - default is no timeout
          - `document_class` (optional): default class to use for
            documents returned from queries on this connection
          - `tz_aware` (optional): if ``True``,
            :class:`~datetime.datetime` instances returned as values
            in a document by this :class:`Connection` will be timezone
            aware (otherwise they will be naive)

        .. seealso:: :meth:`end_request`
        .. versionchanged:: 1.8
           The `host` parameter can now be a full `mongodb URI
           <http://dochub.mongodb.org/core/connections>`_, in addition
           to a simple hostname. It can also be a list of hostnames or
           URIs.
        .. versionadded:: 1.8
           The `tz_aware` parameter.
        .. versionadded:: 1.7
           The `document_class` parameter.
        .. versionchanged:: 1.4
           DEPRECATED The `pool_size`, `auto_start_request`, and `timeout`
           parameters.
        .. versionadded:: 1.1
           The `network_timeout` parameter.

        .. mongodoc:: connections
        """
        if host is None:
            host = self.HOST
        if isinstance(host, basestring):
            host = [host]
        if port is None:
            port = self.PORT
        if not isinstance(port, int):
            raise TypeError("port must be an instance of int")

        nodes = set()
        database = None
        username = None
        password = None
        collection = None
        options = {}
        for uri in host:
            (n, db, u, p, coll, opts) = _parse_uri(uri, port)
            nodes.update(n)
            database = db or database
            username = u or username
            password = p or password
            collection = coll or collection
            options = opts or options
        if not nodes:
            raise ConfigurationError("need to specify at least one host")
        self.__nodes = nodes
        if database and username is None:
            raise InvalidURI("cannot specify database without "
                             "a username and password")

        if pool_size is not None:
            warnings.warn("The pool_size parameter to Connection is "
                          "deprecated", DeprecationWarning)
        if auto_start_request is not None:
            warnings.warn("The auto_start_request parameter to Connection "
                          "is deprecated", DeprecationWarning)
        if timeout is not None:
            warnings.warn("The timeout parameter to Connection is deprecated",
                          DeprecationWarning)

        self.__host = None
        self.__port = None
        
        self.__io_loop = io_loop

        if options.has_key("slaveok"):
            self.__slave_okay = options['slaveok'][0].upper()=='T'
        else:
            self.__slave_okay = slave_okay

        if slave_okay and len(self.__nodes) > 1:
            raise ConfigurationError("cannot specify slave_okay for a paired "
                                     "or replica set connection")

        # TODO - Support using other options like w and fsync from URI
        self.__options = options
        # TODO - Support setting the collection from URI as the Java driver does
        self.__collection = collection

        self.__cursor_manager = CursorManager(self)

        self.__pool = _Pool(self.__connect)
        self.__last_checkout = time.time()

        self.__network_timeout = network_timeout
        self.__document_class = document_class
        self.__tz_aware = tz_aware

        # cache of existing indexes used by ensure_index ops
        self.__index_cache = {}

        if _connect:
            self.__find_master()

        if username:
            database = database or "admin"  
            def auth_err(x):
                if not x: raise ConfigurationError("authentication failed")

            self[database].authenticate(username, password, auth_err)
            

    @classmethod
    def from_uri(cls, uri="mongodb://localhost", **connection_args):
        """DEPRECATED Can pass a mongodb URI directly to Connection() instead.

        .. versionchanged:: 1.8
           DEPRECATED
        .. versionadded:: 1.5
        """
        warnings.warn("Connection.from_uri is deprecated - can pass "
                      "URIs to Connection() now", DeprecationWarning)
        return cls(uri, **connection_args)

    @classmethod
    def paired(cls, left, right=None, **connection_args):
        """DEPRECATED Can pass a list of hostnames to Connection() instead.

        .. versionchanged:: 1.8
           DEPRECATED
        """
        warnings.warn("Connection.paired is deprecated - can pass multiple "
                      "hostnames to Connection() now", DeprecationWarning)
        if isinstance(left, str) or isinstance(right, str):
            raise TypeError("arguments to paired must be tuples")
        if right is None:
            right = (cls.HOST, cls.PORT)
        return cls([":".join(map(str, left)), ":".join(map(str, right))],
                   **connection_args)

    def _cache_index(self, database, collection, index, ttl):
        """Add an index to the index cache for ensure_index operations.

        Return ``True`` if the index has been newly cached or if the index had
        expired and is being re-cached.

        Return ``False`` if the index exists and is valid.
        """
        now = datetime.datetime.utcnow()
        expire = datetime.timedelta(seconds=ttl) + now

        if database not in self.__index_cache:
            self.__index_cache[database] = {}
            self.__index_cache[database][collection] = {}
            self.__index_cache[database][collection][index] = expire
            return True

        if collection not in self.__index_cache[database]:
            self.__index_cache[database][collection] = {}
            self.__index_cache[database][collection][index] = expire
            return True

        if index in self.__index_cache[database][collection]:
            if now < self.__index_cache[database][collection][index]:
                return False

        self.__index_cache[database][collection][index] = expire
        return True

    def _purge_index(self, database_name,
                     collection_name=None, index_name=None):
        """Purge an index from the index cache.

        If `index_name` is None purge an entire collection.

        If `collection_name` is None purge an entire database.
        """
        if not database_name in self.__index_cache:
            return

        if collection_name is None:
            del self.__index_cache[database_name]
            return

        if not collection_name in self.__index_cache[database_name]:
            return

        if index_name is None:
            del self.__index_cache[database_name][collection_name]
            return

        if index_name in self.__index_cache[database_name][collection_name]:
            del self.__index_cache[database_name][collection_name][index_name]

    @property
    def host(self):
        """Current connected host.

        .. versionchanged:: 1.3
           ``host`` is now a property rather than a method.
        """
        return self.__host

    @property
    def port(self):
        """Current connected port.

        .. versionchanged:: 1.3
           ``port`` is now a property rather than a method.
        """
        return self.__port

    @property
    def nodes(self):
        """List of all known nodes.

        Includes both nodes specified when the :class:`Connection` was
        created, as well as nodes discovered through the replica set
        discovery mechanism.

        .. versionadded:: 1.8
        """
        return self.__nodes

    @property
    def slave_okay(self):
        """Is it okay for this connection to connect directly to a slave?
        """
        return self.__slave_okay

    def get_document_class(self):
        return self.__document_class

    def set_document_class(self, klass):
        self.__document_class = klass

    document_class = property(get_document_class, set_document_class,
                              doc="""Default class to use for documents
                              returned on this connection.

                              .. versionadded:: 1.7
                              """)

    @property
    def tz_aware(self):
        """Does this connection return timezone-aware datetimes?

        See the `tz_aware` parameter to :meth:`Connection`.

        .. versionadded:: 1.8
        """
        return self.__tz_aware


    def __find_master(self):
        """
           do it and downstream with callbacks
        """
        # Special case the first node to try to get the primary or any
        # additional hosts from a replSet:
        first = iter(self.__nodes).next()

        callback = self.__callback_master
        self.__try_node(first,callback)
        

    def __try_node(self, node,callback):
        self.disconnect()
        self.__host, self.__port = node
        self.admin.command("ismaster",callback=callback)
        

    def __callback_master(self,response):
           
        primary = self.__add_hosts_and_get_primary(response)
        self.end_request()
        
        if response["ismaster"]:
            primary = True
 
        if (primary is True) or (self.__slave_okay and primary is not None):
            return 
        else:        
            raise AutoReconnect("could not find master/primary")
        

    def __add_hosts_and_get_primary(self, response):
        if "hosts" in response:
            self.__nodes.update([_str_to_node(h) for h in response["hosts"]])
        if "primary" in response:
            return _str_to_node(response["primary"])
        return False


    def __connect(self,callback):
        """(Re-)connect to Mongo and return a new (connected) socket.

        Connect to the master if this is a paired connection.
        """
        host, port = self.__host, self.__port
        
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            stream = tornado.iostream.IOStream(sock,self.__io_loop)
        except:
            self.disconnect()
            raise AutoReconnect("could not connect to %r" % list(self.__nodes))
        else:
            def scallback():
                callback(stream)
            stream.connect((host,port),callback=scallback)
        
                         

    def __stream(self,callback):
        """Get a socket from the pool.

        If it's been > 1 second since the last time we checked out a
        socket, we also check to see if the socket has been closed -
        this let's us avoid seeing *some*
        :class:`~pymongo.errors.AutoReconnect` exceptions on server
        hiccups, etc. We only do this if it's been > 1 second since
        the last socket checkout, to keep performance reasonable - we
        can't avoid those completely anyway.
        """
        
        def scallback(strm):
            t = time.time()
            if t - self.__last_checkout > 1 and strm._check_closed():
                self.disconnect()
                self.__pool.get_stream(scallback)
            else:
                self.__last_checkout = t
                callback(strm)
  
        self.__pool.get_stream(scallback)
        


    def disconnect(self):
        """Disconnect from MongoDB.

        Disconnecting will close all underlying sockets in the
        connection pool. If the :class:`Connection` is used again it
        will be automatically re-opened. Care should be taken to make
        sure that :meth:`disconnect` is not called in the middle of a
        sequence of operations in which ordering is important. This
        could lead to unexpected results.

        .. seealso:: :meth:`end_request`
        .. versionadded:: 1.3
        """
        self.__pool = _Pool(self.__connect)
        self.__host = None
        self.__port = None

    def set_cursor_manager(self, manager_class):
        """Set this connection's cursor manager.

        Raises :class:`TypeError` if `manager_class` is not a subclass of
        :class:`~pymongo.cursor_manager.CursorManager`. A cursor manager
        handles closing cursors. Different managers can implement different
        policies in terms of when to actually kill a cursor that has
        been closed.

        :Parameters:
          - `manager_class`: cursor manager to use
        """
        manager = manager_class(self)
        if not isinstance(manager, CursorManager):
            raise TypeError("manager_class must be a subclass of "
                            "CursorManager")

        self.__cursor_manager = manager

    def __check_response_to_last_error(self, response):
        """Check a response to a lastError message for errors.

        `response` is a byte string representing a response to the message.
        If it represents an error response we raise OperationFailure.

        Return the response as a document.
        """
        response = helpers._unpack_response(response)

        assert response["number_returned"] == 1
        error = response["data"][0]

        response = helpers._check_command_response(error, self.disconnect)

        # TODO unify logic with database.error method
        if error.get("err", 0) is None:
            return error
        if error["err"] == "not master":
            self.disconnect()
            return AutoReconnect("not master")

        if "code" in error:
            if error["code"] in [11000, 11001, 12582]:
                return DuplicateKeyError(error["err"])
            else:
                return OperationFailure(error["err"], error["code"])
        else:
            return OperationFailure(error["err"])
        
        return response

    def _send_message(self, message,with_last_error=False,callback=None):
        """Say something to Mongo.

        Raises ConnectionFailure if the message cannot be sent. Raises
        OperationFailure if `with_last_error` is ``True`` and the
        response to the getLastError call returns an error. Return the
        response from lastError, or ``None`` if `with_last_error`
        is ``False``.

        :Parameters:
          - `message`: message to send
          - `with_last_error`: check getLastError status after sending the
            message
        """

        
        def send_callback(strm): 
            (request_id, data) = message
            try:
                strm.write(data)
            except (ConnectionFailure,socket.error),e:
                self.disconnect()
                raise AutoReconnect(str(e)) 
            else:
                if with_last_error:
                    assert callback != None
                    def mod_callback(resp):
                        resp = self.__check_response_to_last_error(resp)
                        callback(resp)
                        
                    self.__receive_message_on_stream(1,request_d,strm,callback=mod_callback)
                elif callback:
                     callback(None)
    
             
        self.__stream(send_callback)
        

    def __receive_message_on_stream(self, operation, request_id, strm,callback):
        """Receive a message in response to `request_id` on `sock`.

        Returns the response data with the header removed.
        """
     
        receive_body_curry = functools.partial(receive_body_on_stream,operation,request_id,strm,callback)
        strm.read_bytes(16,receive_body_curry)
        
        

    def __send_and_receive(self, message, callback,strm):
        """Send a message on the given socket and return the response data.
        """
        (request_id, data) = message        
        strm.write(data)
        
        self.__receive_message_on_stream(1, request_id, strm, callback)
        


    def _send_message_with_response(self, message, callback):
        """
        """
           
        send_callback = functools.partial(self.__send_and_receive,message,callback)
                     
        self.__stream(send_callback)

                               

    def start_request(self):
        """DEPRECATED all operations will start a request.

        .. versionchanged:: 1.4
           DEPRECATED
        """
        warnings.warn("the Connection.start_request method is deprecated",
                      DeprecationWarning)

    def end_request(self):
        """Allow this thread's connection to return to the pool.

        Calling :meth:`end_request` allows the :class:`~socket.socket`
        that has been reserved for this thread to be returned to the
        pool. Other threads will then be able to re-use that
        :class:`~socket.socket`. If your application uses many
        threads, or has long-running threads that infrequently perform
        MongoDB operations, then judicious use of this method can lead
        to performance gains. Care should be taken, however, to make
        sure that :meth:`end_request` is not called in the middle of a
        sequence of operations in which ordering is important. This
        could lead to unexpected results.

        One important case is when a thread is dying permanently. It
        is best to call :meth:`end_request` when you know a thread is
        finished, as otherwise its :class:`~socket.socket` will not be
        reclaimed.
        """
        self.__pool.return_stream()

    def __cmp__(self, other):
        if isinstance(other, Connection):
            return cmp((self.__host, self.__port),
                       (other.__host, other.__port))
        return NotImplemented

    def __repr__(self):
        if len(self.__nodes) == 1:
            return "Connection(%r, %r)" % (self.__host, self.__port)
        else:
            return "Connection(%r)" % ["%s:%d" % n for n in self.__nodes]

    def __getattr__(self, name):
        """Get a database by name.

        Raises :class:`~pymongo.errors.InvalidName` if an invalid
        database name is used.

        :Parameters:
          - `name`: the name of the database to get
        """
        return database.Database(self, name)

    def __getitem__(self, name):
        """Get a database by name.

        Raises :class:`~pymongo.errors.InvalidName` if an invalid
        database name is used.

        :Parameters:
          - `name`: the name of the database to get
        """
        return self.__getattr__(name)

    def close_cursor(self, cursor_id):
        """Close a single database cursor.

        Raises :class:`TypeError` if `cursor_id` is not an instance of
        ``(int, long)``. What closing the cursor actually means
        depends on this connection's cursor manager.

        :Parameters:
          - `cursor_id`: id of cursor to close

        .. seealso:: :meth:`set_cursor_manager` and
           the :mod:`~pymongo.cursor_manager` module
        """
        if not isinstance(cursor_id, (int, long)):
            raise TypeError("cursor_id must be an instance of (int, long)")

        self.__cursor_manager.close(cursor_id)

    def kill_cursors(self, cursor_ids):
        """Send a kill cursors message with the given ids.

        Raises :class:`TypeError` if `cursor_ids` is not an instance of
        ``list``.

        :Parameters:
          - `cursor_ids`: list of cursor ids to kill
        """
        if not isinstance(cursor_ids, list):
            raise TypeError("cursor_ids must be a list")
        self._send_message(message.kill_cursors(cursor_ids))

    def server_info(self,callback):
        """Get information about the MongoDB server we're connected to.
        """
        self.admin.command("buildinfo",callback)

    def database_names(self,callback):
        """Get a list of the names of all databases on the connected server.
        """
        
        def ncallback(resp):
            nlist = [db["name"] for db in resp["databases"]]
            return callback(nlist)
            
        self.admin.command("listDatabases",ncallback)
        
        
    def drop_database(self, name_or_database):
        """Drop a database.

        Raises :class:`TypeError` if `name_or_database` is not an instance of
        ``(str, unicode, Database)``

        :Parameters:
          - `name_or_database`: the name of a database to drop, or a
            :class:`~pymongo.database.Database` instance representing the
            database to drop
        """
        name = name_or_database
        if isinstance(name, database.Database):
            name = name.name

        if not isinstance(name, basestring):
            raise TypeError("name_or_database must be an instance of "
                            "(Database, str, unicode)")

        self._purge_index(name)
        self[name].command("dropDatabase")
        

    def copy_database(self, from_name, to_name,callback=None,
                      from_host=None, username=None, password=None):
        """Copy a database, potentially from another host.

        Raises :class:`TypeError` if `from_name` or `to_name` is not
        an instance of :class:`basestring`. Raises
        :class:`~pymongo.errors.InvalidName` if `to_name` is not a
        valid database name.

        If `from_host` is ``None`` the current host is used as the
        source. Otherwise the database is copied from `from_host`.

        If the source database requires authentication, `username` and
        `password` must be specified.

        :Parameters:
          - `from_name`: the name of the source database
          - `to_name`: the name of the target database
          - `from_host` (optional): host name to copy from
          - `username` (optional): username for source database
          - `password` (optional): password for source database

        .. note:: Specifying `username` and `password` requires server
           version **>= 1.3.3+**.

        .. versionadded:: 1.5
        """
        if not isinstance(from_name, basestring):
            raise TypeError("from_name must be an instance of basestring")
        if not isinstance(to_name, basestring):
            raise TypeError("to_name must be an instance of basestring")

        database._check_name(to_name)

        command = {"fromdb": from_name, "todb": to_name}

        if from_host is not None:
            command["fromhost"] = from_host

#         if username is not None:
#             nonce = self.admin.command("copydbgetnonce",
#                                        fromhost=from_host)["nonce"]
#             command["username"] = username
#             command["nonce"] = nonce
#             command["key"] = helpers._auth_key(nonce, username, password)

        self.admin.command("copydb",callback=callback, **command)


    def __iter__(self):
        return self

    def next(self):
        raise TypeError("'Connection' object is not iterable")
