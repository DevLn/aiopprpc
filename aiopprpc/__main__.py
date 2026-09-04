"""``python -m aiopprpc [camera-ip]`` -- discover cameras and serve them at :4000.

Close the vendor app first: the camera accepts only one client at a time.

A session is created the moment a camera answers discovery, because the reply
carries the one-shot ``lsign`` that LanAuth needs. When a session is lost it is
simply forgotten; the next discovery cycle finds the camera again with a fresh
credential and reconnects it.
"""
import argparse
import asyncio
import logging
import time

from .discover import DEFAULT_DISCOVERY_ADDRESS, Discovery
from .http_server import SESSIONS, start_web_server
from .session import make_session

logger = logging.getLogger(__name__)

tasks = {}
new_device_fut = None


def notify_new_device():
    fut = new_device_fut
    if fut is not None and not fut.done():
        fut.set_result(None)


def on_device_found(device):
    if device.dev_id in SESSIONS:
        # Discovery re-finds connected cameras every cycle; making a second
        # session here would leak the first one's tasks.
        return
    if not device.lsign:
        logger.warning('%s answered without an lsign; cannot authenticate', device)
        return
    session = make_session(device, on_device_lost=on_device_lost,
                           on_event=lambda name, payload: logger.info(
                               'event %s from %s: %s', name, device.dev_id, payload))
    SESSIONS[device.dev_id] = session
    session.start()
    tasks[device.dev_id] = session.running_tasks()
    notify_new_device()


def on_device_lost(device):
    logger.warning('Device %s lost; discovery will pick it up again', device.dev_id)
    SESSIONS.pop(device.dev_id, None)
    tasks.pop(device.dev_id, None)
    notify_new_device()


async def amain(remote_addr, local_port, http_port, period):
    global new_device_fut
    discovery = Discovery(remote_addr=remote_addr, local_port=local_port)

    discovery_task = asyncio.create_task(discovery.discover(on_device_found, period=period))
    webserver_task = asyncio.create_task(start_web_server(http_port))
    logger.info('Open http://localhost:%d/ once a camera is found', http_port)
    try:
        while True:
            new_device_fut = asyncio.Future()
            device_tasks = {t for group in tasks.values() for t in group}
            done, pending = await asyncio.wait(
                [discovery_task, webserver_task, new_device_fut, *device_tasks],
                return_when=asyncio.FIRST_COMPLETED,
            )
            if new_device_fut in pending:
                # Discovery or the web server ended: stop supervising.
                break
        for task in done:
            # Never swallow why supervision ended. A dead supervisor task is
            # almost always something the operator must see -- the commonest
            # is the web port already being held by an earlier run, which
            # otherwise looks like "the server shuts down right after start".
            if task.cancelled():
                continue
            err = task.exception()
            if err is not None:
                logger.error('%s stopped: %r', task.get_coro().__qualname__, err)
    finally:
        for session in list(SESSIONS.values()):
            session.stop()
        SESSIONS.clear()
        discovery_task.cancel()
        webserver_task.cancel()


#: Timestamps to the millisecond: when the camera reboots, what matters is the
#: gap between the last thing we sent and the connection dropping.
LOG_FORMAT = '%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s'
LOG_DATEFMT = '%H:%M:%S'


def configure_logging(level, path=None):
    handlers = [logging.StreamHandler()]
    if path:
        # Append rather than truncate: a crash-and-retry cycle would otherwise
        # destroy the log of the run that actually reproduced the problem.
        handlers.append(logging.FileHandler(path, mode='a', encoding='utf-8'))
    logging.basicConfig(level=logging.getLevelName(level.upper()),
                        format=LOG_FORMAT, datefmt=LOG_DATEFMT, handlers=handlers)
    if path:
        logging.getLogger(__name__).info(
            '=== aiopprpc run started %s, logging to %s ===',
            time.strftime('%Y-%m-%d %H:%M:%S'), path)


def main():
    parser = argparse.ArgumentParser(
        prog='aiopprpc',
        description='A test web server for PPRPC (XC Things / FtyCam) cameras',
    )
    parser.add_argument(
        'host', nargs='?', default=None,
        help='Camera IP. Discovery is sent there and to its subnet broadcast; '
             'omit to broadcast on the whole network.',
    )
    parser.add_argument(
        '-a', '--addr', type=str, default=None,
        help=f'Discovery address, default is {DEFAULT_DISCOVERY_ADDRESS} '
             '(overridden by the positional host)',
    )
    parser.add_argument('--http', type=int, default=4000, help='Web server port, default 4000')
    parser.add_argument(
        '-dp', '--local-discovery-port', type=int, default=0,
        help='Local UDP port to receive discovery replies on, default is random',
    )
    parser.add_argument('--discovery-period', type=float, default=10,
                        help='Seconds between discovery broadcasts, default 10')
    parser.add_argument('--log-level', type=str, default='INFO')
    parser.add_argument(
        '--log-file', type=str, default=None,
        help='Also append the log to this file. Use with --log-level DEBUG to '
             'capture every packet for post-mortem analysis -- the console '
             'scrollback is lost when the camera drops the connection.',
    )

    args = parser.parse_args()
    configure_logging(args.log_level, args.log_file)
    try:
        asyncio.run(amain(
            remote_addr=args.host or args.addr or DEFAULT_DISCOVERY_ADDRESS,
            local_port=args.local_discovery_port,
            http_port=args.http,
            period=args.discovery_period,
        ))
    except KeyboardInterrupt:
        logger.info('Stopped')


if __name__ == '__main__':
    main()
