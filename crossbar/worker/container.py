#####################################################################################
#
#  Copyright (c) Crossbar.io Technologies GmbH
#
#  Unless a separate license agreement exists between you and Crossbar.io GmbH (e.g.
#  you have purchased a commercial license), the license terms below apply.
#
#  Should you enter into a separate license agreement after having received a copy of
#  this software, then the terms of such license agreement replace the terms below at
#  the time at which such license agreement becomes effective.
#
#  In case a separate license agreement ends, and such agreement ends without being
#  replaced by another separate license agreement, the license terms below apply
#  from the time at which said agreement ends.
#
#  LICENSE TERMS
#
#  This program is free software: you can redistribute it and/or modify it under the
#  terms of the GNU Affero General Public License, version 3, as published by the
#  Free Software Foundation. This program is distributed in the hope that it will be
#  useful, but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#
#  See the GNU Affero General Public License Version 3 for more details.
#
#  You should have received a copy of the GNU Affero General Public license along
#  with this program. If not, see <http://www.gnu.org/licenses/agpl-3.0.en.html>.
#
#####################################################################################

from __future__ import absolute_import

from functools import partial
from datetime import datetime

from twisted import internet
from twisted.internet.defer import Deferred, DeferredList, inlineCallbacks
from twisted.internet.defer import returnValue
from twisted.python.failure import Failure

from autobahn.util import utcstr
from autobahn.wamp.exception import ApplicationError
from autobahn.wamp.types import ComponentConfig, PublishOptions
from autobahn.wamp.types import RegisterOptions

from crossbar.common import checkconfig
from crossbar.worker import _appsession_loader
from crossbar.worker.worker import NativeWorkerSession
from crossbar.router.protocol import WampWebSocketClientFactory, \
    WampRawSocketClientFactory, WampWebSocketClientProtocol, WampRawSocketClientProtocol

from crossbar.twisted.endpoint import create_connecting_endpoint_from_config

__all__ = ('ContainerWorkerSession',)


class ContainerComponent(object):
    """
    An application component running inside a container.

    This class is for _internal_ use within ContainerWorkerSession.
    """

    def __init__(self, id, config, proto, session):
        """
        Ctor.

        :param id: The ID of the component within the container.
        :type id: int
        :param config: The component configuration the component was created from.
        :type config: dict
        :param proto: The transport protocol instance the component runs for talking
                      to the application router.
        :type proto: instance of CrossbarWampWebSocketClientProtocol or CrossbarWampRawSocketClientProtocol
        :param session: The application session of this component.
        :type session: Instance derived of ApplicationSession.
        """
        self.started = datetime.utcnow()
        self.id = id
        self.config = config
        self.proto = proto
        self.session = session

        # internal use; see e.g. restart_container_component
        self._stopped = Deferred()

    def marshal(self):
        """
        Marshal object information for use with WAMP calls/events.
        """
        now = datetime.utcnow()
        return {
            u'id': self.id,
            u'started': utcstr(self.started),
            u'uptime': (now - self.started).total_seconds(),
            u'config': self.config
        }


class ContainerWorkerSession(NativeWorkerSession):
    """
    A container is a native worker process that hosts application components
    written in Python. A container connects to an application router (creating
    a WAMP transport) and attached to a given realm on the application router.
    """
    WORKER_TYPE = u'container'

    def __init__(self, config=None, reactor=None):
        NativeWorkerSession.__init__(self, config, reactor)

        # map: component ID -> ContainerComponent
        self.components = {}

        # "global" shared between all components
        self.components_shared = {
            u'reactor': reactor
        }

    @inlineCallbacks
    def onJoin(self, details):
        """
        Called when worker process has joined the node's management realm.
        """
        self.log.info('Container worker "{worker_id}" session {session_id} initializing ..', worker_id=self._worker_id, session_id=details.session)

        yield NativeWorkerSession.onJoin(self, details, publish_ready=False)

        # the procedures registered
        procs = [
            u'stop_worker',
            u'start_component',
            u'stop_component',
            u'restart_component',
            u'get_component',
            u'list_components',
        ]

        dl = []
        for proc in procs:
            uri = u'{}.{}'.format(self._uri_prefix, proc)
            self.log.debug('Registering management API procedure <{proc}>', proc=uri)
            dl.append(self.register(getattr(self, proc), uri, options=RegisterOptions(details_arg='details')))

        regs = yield DeferredList(dl)

        self.log.debug('Ok, registered {cnt} management API procedures', cnt=len(regs))

        self.log.info('Container worker "{worker_id}" session ready', worker_id=self._worker_id)

        # NativeWorkerSession.publish_ready()
        yield self.publish_ready()

    @inlineCallbacks
    def stop_worker(self, details=None):
        """
        Stops the whole container.
        """
        stopped_components = []
        dl = []
        for component in self.components:
            dl.append(self.stop_container_component(component.id))
            stopped_components.append(component.id)
        yield DeferredList(dl)
        yield self.disconnect()
        returnValue(stopped_components)

    def start_component(self, id, config, reload_modules=False, details=None):
        """
        Starts a Class or WAMPlet in this component container.

        :param config: Component configuration.
        :type config: dict
        :param reload_modules: If `True`, enforce reloading of modules (user code)
                               that were modified (see: TrackingModuleReloader).
        :type reload_modules: bool
        :param details: Caller details.
        :type details: instance of :class:`autobahn.wamp.types.CallDetails`

        :returns dict -- A dict with combined info from component starting.
        """
        self.log.debug("{klass}.start_container_component({id}, {config})",
                       klass=self.__class__.__name__, id=id, config=config)

        # prohibit starting a component twice
        #
        if id in self.components:
            emsg = "Could not start component - a component with ID '{}'' is already running (or starting)".format(id)
            self.log.error(emsg)
            raise ApplicationError(u'crossbar.error.already_running', emsg)

        # check configuration
        #
        try:
            checkconfig.check_container_component(config)
        except Exception as e:
            emsg = "Invalid container component configuration ({})".format(e)
            self.log.error(emsg)
            raise ApplicationError(u"crossbar.error.invalid_configuration", emsg)
        else:
            self.log.debug("Starting {type}-component in container.",
                           type=config['type'])

        # 1) create WAMP application component factory
        #
        realm = config['realm']
        extra = config.get('extra', None)
        component_config = ComponentConfig(realm=realm,
                                           extra=extra,
                                           keyring=None,
                                           controller=self if self.config.extra.expose_controller else None,
                                           shared=self.components_shared if self.config.extra.expose_shared else None)
        try:
            create_component = _appsession_loader(config)
        except ApplicationError as e:
            self.log.error("Component loading failed", log_failure=Failure())
            if 'No module named' in str(e):
                self.log.error("  Python module search paths:")
                for path in e.kwargs['pythonpath']:
                    self.log.error("    {path}", path=path)
            raise

        # force reload of modules (user code)
        #
        if reload_modules:
            self._module_tracker.reload()

        # WAMP application session factory
        # ultimately, this gets called once the connection is
        # establised, from onOpen in autobahn/wamp/websocket.py:59
        def create_session():
            try:
                session = create_component(component_config)

                # any exception spilling out from user code in onXXX handlers is fatal!
                def panic(fail, msg):
                    self.log.error(
                        "Fatal error in component: {msg} - {log_failure.value}",
                        msg=msg, log_failure=fail,
                    )
                    session.disconnect()
                session._swallow_error = panic
                return session
            except Exception:
                self.log.failure("Component instantiation failed: {log_failure.value}")
                raise

        # 2) create WAMP transport factory
        #
        transport_config = config['transport']

        # WAMP-over-WebSocket transport
        #
        if transport_config['type'] == 'websocket':

            # create a WAMP-over-WebSocket transport client factory
            #
            transport_factory = WampWebSocketClientFactory(create_session, transport_config['url'])
            transport_factory.noisy = False

        # WAMP-over-RawSocket transport
        #
        elif transport_config['type'] == 'rawsocket':

            transport_factory = WampRawSocketClientFactory(create_session,
                                                           transport_config)
            transport_factory.noisy = False

        else:
            # should not arrive here, since we did `check_container_component()`
            raise Exception("logic error")

        # 3) create and connect client endpoint
        #
        endpoint = create_connecting_endpoint_from_config(transport_config['endpoint'],
                                                          self.config.extra.cbdir,
                                                          self._reactor,
                                                          self.log)

        # now connect the client
        #
        d = endpoint.connect(transport_factory)

        def success(proto):
            component = ContainerComponent(id, config, proto, None)
            self.components[id] = component

            # FIXME: this is a total hack.
            #
            def close_wrapper(orig, was_clean, code, reason):
                """
                Wrap our protocol's onClose so we can tell when the component
                exits.
                """
                r = orig(was_clean, code, reason)
                if component.id not in self.components:
                    self.log.warn("Component '{id}' closed, but not in set.",
                                  id=component.id)
                    return r

                if was_clean:
                    self.log.info("Closed connection to '{id}' with code '{code}'",
                                  id=component.id, code=code)
                else:
                    self.log.error("Lost connection to component '{id}' with code '{code}'.",
                                   id=component.id, code=code)

                if reason:
                    self.log.warn(str(reason))

                del self.components[component.id]
                self._publish_component_stop(component)
                component._stopped.callback(component.marshal())

                if not self.components:
                    self.log.info("Container is hosting no more components: shutting down.")
                    self.stop_container()

                return r

            # FIXME: due to history, the following is currently the case:
            # ITransportHandler.onClose is implemented directly on WampWebSocketClientProtocol,
            # while with WampRawSocketClientProtocol, the ITransportHandler is implemented
            # by the object living on proto._session
            #
            if isinstance(proto, WampWebSocketClientProtocol):
                proto.onClose = partial(close_wrapper, proto.onClose)

            elif isinstance(proto, WampRawSocketClientProtocol):
                # FIXME: doesn't work without guard, since proto_.session is not yet there when
                # proto comes into existance ..
                if proto._session:
                    proto._session.onClose = partial(close_wrapper, proto._session.onClose)
            else:
                raise Exception("logic error")

            # publish event "on_component_start" to all but the caller
            #
            topic = self._uri_prefix + '.container.on_component_start'
            event = {u'id': id}
            self.publish(topic, event, options=PublishOptions(exclude=details.caller))
            return event

        def error(err):
            # https://twistedmatrix.com/documents/current/api/twisted.internet.error.ConnectError.html
            if isinstance(err.value, internet.error.ConnectError):
                emsg = "Could not connect container component to router - transport establishment failed ({})".format(err.value)
                self.log.error(emsg)
                raise ApplicationError(u'crossbar.error.cannot_connect', emsg)
            else:
                # should not arrive here (since all errors arriving here should be subclasses of ConnectError)
                raise err

        d.addCallbacks(success, error)

        return d

    def _publish_component_stop(self, component):
        """
        Internal helper to publish details to on_component_stop
        """
        event = component.marshal()
        if self.is_connected():
            topic = self._uri_prefix + '.container.on_component_stop'
            # XXX just ignoring a Deferred here...
            self.publish(topic, event)
        return event

    @inlineCallbacks
    def restart_component(self, component_id, reload_modules=False, details=None):
        """
        Restart a component currently running within this container using the
        same configuration that was used when first starting the component.

        :param component_id: The ID of the component to restart.
        :type component_id: str

        :param reload_modules: If `True`, enforce reloading of modules (user code)
                               that were modified (see: TrackingModuleReloader).
        :type reload_modules: bool

        :param details: Caller details.
        :type details: instance of :class:`autobahn.wamp.types.CallDetails`

        :returns dict -- A dict with combined info from component stopping/starting.
        """
        if component_id not in self.components:
            raise ApplicationError(u'crossbar.error.no_such_object', 'no component with ID {} running in this container'.format(component_id))

        component = self.components[component_id]

        stopped = yield self.stop_container_component(component_id, details=details)
        started = yield self.start_container_component(component_id, component.config, reload_modules=reload_modules, details=details)

        del stopped[u'caller']
        del started[u'caller']

        restarted = {
            u'stopped': stopped,
            u'started': started,
            u'caller': {
                u'session': details.caller,
                u'authid': details.caller_authid,
                u'authrole': details.caller_authrole,
            }
        }

        self.publish(u'{}.on_component_restarted'.format(self._uri_prefix),
                     restarted,
                     options=PublishOptions(exclude=details.caller))

        returnValue(restarted)

    @inlineCallbacks
    def stop_component(self, component_id, details=None):
        """
        Stop a component currently running within this container.

        :param component_id: The ID of the component to stop.
        :type component_id: int

        :param details: Caller details.
        :type details: instance of :class:`autobahn.wamp.types.CallDetails`

        :returns: Stop information.
        :rtype: dict
        """
        self.log.debug('{klass}.stop_component({component_id}, {details})', klass=self.__class__.__name__, component_id=component_id, details=details)

        if component_id not in self.components:
            raise ApplicationError(u'crossbar.error.no_such_object', 'no component with ID {} running in this container'.format(component_id))

        component = self.components[component_id]

        try:
            component.proto.close()
        except:
            self.log.failure("failed to close protocol on component '{component_id}': {log_failure}", component_id=component_id)
            raise
        else:
            # essentially just waiting for "on_component_stop"
            yield component._stopped

        stopped = {
            u'component_id': component_id,
            u'uptime': (datetime.utcnow() - component.started).total_seconds(),
            u'caller': {
                u'session': details.caller,
                u'authid': details.caller_authid,
                u'authrole': details.caller_authrole,
            }
        }

        del self.components[component_id]

        self.publish(u'{}.on_component_stopped'.format(self._uri_prefix),
                     stopped,
                     options=PublishOptions(exclude=details.caller))

        returnValue(stopped)

    def get_component(self, component_id, details=None):
        """
        Get a component currently running within this container.

        :param component_id: The ID of the component to get.
        :type component_id: str

        :param details: Caller details.
        :type details: instance of :class:`autobahn.wamp.types.CallDetails`

        :returns: Component detail information.
        :rtype: dict
        """
        self.log.debug('{klass}.get_component({component_id}, {details})', klass=self.__class__.__name__, component_id=component_id, details=details)

        if component_id not in self.components:
            raise ApplicationError(u'crossbar.error.no_such_object', 'no component with ID {} running in this container'.format(component_id))

        return self.components[component_id].marshal()

    def list_components(self, ids_only=True, details=None):
        """
        Get components currently running within this container.

        :param ids_only: If `True`, only return (sorted) list of component IDs.
        :type ids_only: bool

        :param details: Caller details.
        :type details: instance of :class:`autobahn.wamp.types.CallDetails`

        :returns: Plain (sorted) list of component IDs, or list of components
            sorted by component ID when `ids_only==True`.
        :rtype: list
        """
        self.log.debug('{klass}.get_components({details})', klass=self.__class__.__name__, details=details)

        if ids_only:
            return sorted(self.components.keys())
        else:
            res = []
            for component_id in sorted(self.components.keys()):
                res.append(self.components[component_id].marshal())
            return res
