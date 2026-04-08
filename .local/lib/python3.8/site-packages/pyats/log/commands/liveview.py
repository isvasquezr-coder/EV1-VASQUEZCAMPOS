import asyncio
import logging
import socketio
import traceback

from aiohttp import web

from pyats.reporter import ReportClient

from .parser.live import SectionHandler
from .archive import Archive, RunInfoFileHandler

logger = logging.getLogger(__name__)


class LiveViewSIO(web.Application):

    def __init__(self,
                 runinfo_dir=None,
                 keep_alive=False,
                 **kwargs):

        # init application
        super().__init__(**kwargs)

        if runinfo_dir:
            # runinfo directory for current pyats job
            self.runinfo_dir = runinfo_dir
            self.keep_alive = keep_alive

            self.runinfo_handler = RunInfoFileHandler(runinfo_dir)
            self.section_handler = SectionHandler()
            # track the file/section we are currently tailing
            self.tailing_records = {}

            # create a socket.IO server and attach it to aiohttp app
            self.sio = socketio.AsyncServer(async_mode='aiohttp',
                                            logger=False, engineio_logger=False,
                                            cors_allowed_origins='*')
            self.sio.attach(self)

            # mute loggers
            for logger_name in ('aiohttp', 'socketio', 'engineio'):
                mylogger = logging.getLogger(logger_name)
                mylogger.setLevel(logging.root.level + 10)

            # register background task
            self.on_startup.append(self.connect_and_subscribe)
            self.on_cleanup.append(self.unsubscribe_and_disconnect)

            # register socket event handler
            self.sio.on('liveview', handler=self.liveview,
                        namespace='/liveview')
            self.sio.on('disconnect_request', handler=self.disconnect_request,
                        namespace='/liveview')
            self.sio.on('connect', handler=self.connect,
                        namespace='/liveview')
            self.sio.on('disconnect', handler=self.disconnect,
                        namespace='/liveview')

    @web.middleware
    async def check_live_middleware(self, request, handler):
        try:
            resp = await handler(request)
            return resp
        except AttributeError as e:
            if str(e) == "'LogViewerServer' object has no attribute 'archive'":
                response_obj = {'detail': 'Job is still running, please try \
                            liveview with socket connetion',
                            'status': 'running',
                            }
                return web.json_response(response_obj,
                                        headers=self._default_header,
                                        status=400)
            else:
                response_obj = {'detail': str(e)}
                return web.json_response(response_obj,
                                        headers = self._default_header,
                                        status=500)

    async def liveview_tasklog(self, sid, message):
        ''' Tail task log file '''
        task_filename = message.get('data', 'TaskLog.Task-1')
        self._start_tailing(sid, task_filename, 'tasklog')
        async for line in self.runinfo_handler.tailing_file(task_filename):
            if self._check_stop(sid, task_filename ,'tasklog'):
                break
            await self.sio.emit('liveview',
                                {
                                    'cmd': 'tasklog',
                                    'data': line,
                                    'task_name': task_filename
                                },
                                namespace='/liveview',
                                room=sid)

    async def unsubscribe_and_disconnect(self, app):
        self.reportclient.subscribed = False
        self.reportclient.close()
        # clean up archive
        if hasattr(self, 'archive') and self.archive:
            logger.debug('Clean up unzipped archive file.')
            self.archive.__exit__(None, None, None)

    async def connect_and_subscribe(self, app):
        ''' Connect to easypy reporter server,
            subscribe to the callback event for handling each section
            also fetch the entire testsuite to add any previously run sections
        '''
        self.reportclient = ReportClient(
                socketpath=self.runinfo_handler.socketpath)
        self.reportclient.connect()
        # schedule getting the testsuite and subscribing to easypy reporter
        asyncio.ensure_future(self.reportclient.subscribe(self.print_section))
        asyncio.ensure_future(self.get_testsuite_impl())

    async def print_section(self, section):
        ''' process and emit each section '''
        parsed_section = self.section_handler.parse_section(section)
        await self.sio.emit('liveview',
                            {'cmd': 'section', 'data': parsed_section},
                            namespace='/liveview')
        if parsed_section['event'].startswith('stop') and \
            parsed_section['id'] in self.tailing_records.get('section_log',{}):

            for item in self.tailing_records['section_log'][
                parsed_section['id']].values():

                # mark a section as finished, log tailing should stop
                item['stop_tailing'] = True
                item['data'] = parsed_section

        if section['event'] == 'stop_testsuite':
            if self.keep_alive:
                # try to get the new generated archive file of this job
                archive_dir = None
                for retry in range(10):
                    try:
                        archive_dir=list(
                            (self.runinfo_dir.parent.parent / 'archive').glob(
                            '**/{}.zip'.format(self.runinfo_dir.name)))[0]
                        if archive_dir:
                            break
                    except:
                        await asyncio.sleep(0.5)
                if archive_dir:
                    self.archive=Archive(archive_dir)
                    # unzip the file for logviewer
                    self.archive.__enter__()
            # send terminate event, UI will terminate socket connection
            await self.sio.emit('terminate', {}, namespace='/liveview')
            # close connection to easypy reporter
            self.reportclient.close()

    async def liveview_section_log(self, sid, message):
        ''' When a section is running, tail the section log.
            When a section finishes, get the whole section log.
        '''
        section = message.get('data')
        section_id = str(section.get('id'))
        if section.get('result') and section['result'] == 'running' or \
            section.get('stop_time') is None:
            # tail the log file
            self._start_tailing(sid, section_id, 'section_log')
            async for line in self.runinfo_handler.tailing_file(
                section.get('logs', {}).get('file'),
                start_bytes=section.get('logs', {}).get('begin')):

                data = self._check_stop(sid, section_id ,'section_log')
                if data:
                    if isinstance(data, dict) and data.get('data'):
                        # stop tailing since section finishes running.
                        # should emit the whole section log later.
                        section = data['data']
                    else:
                        # stop tailing since frontend asked to stop
                        pass
                    break
                await self.sio.emit('liveview',
                                    {
                                        'cmd': 'section_log',
                                        'data': line,
                                        'section_id': section_id,
                                        'status': 'running'
                                    },
                                    namespace='/liveview',
                                    room=sid)

        if not section['result'] == 'running':
            # read section log by bytes offset and size within task log file

            if section.get('logs', {}).get('file', None) is None:
                assert Exception('Logs file of section {} is not provided.'\
                    .format(section_id))

            file_path = self.runinfo_dir / section.get('logs', {}).get('file')
            if not file_path.exists():
                file_path = section.get('logs', {}).get('file')

            logs = self.runinfo_handler.read_bytes(
                str(file_path),
                log_start=section.get('logs', {}).get('begin'),
                log_size=section.get('logs', {}).get('size'),
            )
            await self.sio.emit('liveview',
                                {
                                    'cmd': 'section_log',
                                    'data': logs,
                                    'section_id': section_id,
                                    'status': 'finished',

                                },
                                namespace='/liveview',
                                room=sid)

    async def liveview_files(self, sid, message):
        ''' list all files under runinfo folder
            When it's connected to pyats easypy plugins (run is not complete)
            check the folder every 10 seconds
        '''
        while self.reportclient._socket:
            result = list(self.runinfo_handler.listdir())
            await self.sio.emit('liveview',
                                {'cmd': 'files', 'data': result},
                                namespace='/liveview',
                                room=sid)
            await asyncio.sleep(10)

    async def liveview_readfile(self, sid, message):
        ''' Tail a file in runinfo folder'''
        filename = message.get('data', 'TaskLog.Task-1')
        self._start_tailing(sid, filename, 'readfile')
        async for line in self.runinfo_handler.tailing_file(filename):
            if self._check_stop(sid, filename ,'readfile'):
                break
            await self.sio.emit('liveview',
                                {
                                    'cmd': 'readfile',
                                    'data': line,
                                    'filename': filename
                                },
                                namespace='/liveview',
                                room=sid)

    def _start_tailing(self, sid, name, event_name):
        ''' When start tailing a file, add it to self.tailing_records.

            Sample of self.tailing_records.
            {
                "event_name": {
                    "filename/section_id": {
                        "session_id": {
                            "stop_tailing": True/False
                                            (Should stop/ shouldn't stop)
                        }
                    }
                },
                "tasklog": {
                    "TaskLog.Task-1": {
                        "a2b63a7dbe894963b70de0d7a02a498d": {
                            "stop_tailing": False
                        }
                    }
                },
                "readfile": {
                    "env.txt": {
                        "a2b63a7dbe894963b70de0d7a02a498d": {
                            "stop_tailing": True
                        }
                    }
                },
                "section_log": {
                    "11": {
                        "c4fcc081ed554a799d25572858eb2ce7": {
                            "stop_tailing": True,
                            "data": {
                                # any extra data here
                            }
                        }
                    }
                }
            }
        '''
        self.tailing_records.setdefault(event_name, {}).setdefault(\
            name, {})[sid] = {'stop_tailing': False}

    async def liveview_stop_tailing(self, sid, message):
        ''' listen to stop tailing event from UI
        '''
        try:
            _data = message.get('data')
            if _data:
                self.tailing_records[_data['event_name']][_data['name']]\
                    [sid]['stop_tailing']=True
        except Exception:
            pass

    def _check_stop(self, sid, name, event_name):
        ''' check if we should stop tailing the tasklog/sectionlog/file
            return
                False: should not stop
                True or data in dict: should stop
        '''
        _data = self.tailing_records.get(event_name,{}).get(name,{})
        if _data and sid in _data and _data[sid]['stop_tailing'] == True:
            # should stop
            return _data.pop(sid)
        else:
            # should continue to run
            return False

    async def get_testsuite_impl(self):
        '''Get the full result data from execution so far to update sections'''
        loop = asyncio.get_event_loop()
        testsuite = await loop.run_in_executor(None,
                                               self.reportclient.get_testsuite)
        return self.section_handler.parse_testsuite(testsuite)

    async def liveview_get_testsuite(self, sid, message):
        '''Get the full result data from execution so far and emit'''
        parsed_testsuite = await self.get_testsuite_impl()
        await self.sio.emit('liveview',
                            {'cmd': 'get_testsuite',
                             'data': {'event': 'get_testsuite',
                                      'sections': parsed_testsuite}},
                            namespace='/liveview',
                            room=sid)

    async def liveview(self, sid, message):
        ''' Handle client sent events.
            Supported cmd includes: tasklog, section_log, files, readfile
        '''
        cmd = message.get('cmd')
        try:
            if not cmd:
                logger.debug('No cmd provided.')
                await self.sio.emit('liveview',
                                    {'cmd': 'error',
                                     'data': 'No cmd provided.'},
                                    namespace='/liveview',
                                    room=sid,
                                    )
            elif cmd == 'tasklog':
                await self.liveview_tasklog(sid, message)
            elif cmd == 'section_log':
                await self.liveview_section_log(sid, message)
            elif cmd == 'files':
                await self.liveview_files(sid, message)
            elif cmd == 'readfile':
                await self.liveview_readfile(sid, message)
            elif cmd == 'get_testsuite':
                await self.liveview_get_testsuite(sid, message)
            elif cmd == 'stop_tailing':
                await self.liveview_stop_tailing(sid, message)
            else:
                logger.debug('cmd {} is not supported.'.format(cmd))
                await self.sio.emit('liveview',
                                    {'cmd': 'error',
                                     'data': 'cmd {} is not supported.'\
                                             .format(cmd)
                                    },
                                    namespace='/liveview',
                                    room=sid)
        except Exception as e:
            msg = 'Encounter exception while handling cmd {}.'.format(cmd)
            logger.debug(msg)
            logger.debug(traceback.format_exc())
            await self.sio.emit('liveview',
                                {
                                    'cmd': 'error',
                                    'received_cmd': cmd,
                                    'data': msg + ' ' + str(e)
                                },
                                namespace='/liveview',
                                room=sid)

    async def disconnect_request(self, sid):
        await self.sio.disconnect(sid)

    async def connect(self, sid, environ):
        ''' reserved connect event'''
        await self.sio.emit('liveview',
                            {'cmd': 'connect_successfully',
                             'data': 'Connected'},
                            namespace='/liveview',
                            room=sid)

    def disconnect(self, sid):
        ''' reserved disconnect event'''
        pass

