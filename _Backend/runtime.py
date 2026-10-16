"""One managed application per workspace, with explicit port conflict detection."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
from shop_clock import datetime

from config import BASE_DIR


def version():
    digest=hashlib.sha256()
    for name in ('_Backend','_Frontend'):
        for path in sorted((Path(BASE_DIR)/name).rglob('*')):
            if path.is_file() and path.suffix in ('.py','.html','.js','.css'):
                digest.update(str(path.relative_to(BASE_DIR)).encode())
                digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


@contextmanager
def instance_lock():
    root=Path(os.environ.get('MD57_RUNTIME_DIR',str(Path(BASE_DIR)/'.runtime')))
    root.mkdir(exist_ok=True)
    stream=(root/'server.lock').open('a+b')
    if os.fstat(stream.fileno()).st_size==0:
        stream.write(b'0'); stream.flush()
    stream.seek(0)
    try:
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise RuntimeError('MD57 is already running from this workspace. Use manage-server.ps1 status or restart.') from None
    try:
        yield root
    finally:
        stream.close()


def serve(application=None,host='127.0.0.1',port=5000):
    with instance_lock() as root:
        with socket.socket() as probe:
            if os.name=='nt':
                probe.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
            try:
                probe.bind((host,port))
            except OSError:
                raise RuntimeError(f'Port {port} is occupied. Stop its existing server before starting MD57.') from None
        from waitress import create_server
        if application is None:
            from app import app as application
        info={'pid':os.getpid(),'port':port,'host':host,'version':version(),
              'workspace':str(Path(BASE_DIR).resolve()),'started_at':datetime.now().isoformat(timespec='seconds')}
        application.config['RUNTIME_INFO']=info
        # SSE streams occupy request threads; leave capacity for ordinary requests.
        trusted_proxy=os.environ.get('MD57_TRUSTED_PROXY')
        proxy_settings=dict(trusted_proxy=trusted_proxy,trusted_proxy_count=1,
                            trusted_proxy_headers={'x-forwarded-for','x-forwarded-host','x-forwarded-proto','x-forwarded-port'}) if trusted_proxy else {}
        server=create_server(application,host=host,port=port,threads=32,**proxy_settings)
        state=root/'server.json'
        pending=root/'server.json.tmp'
        pending.write_text(json.dumps(info),encoding='utf8')
        pending.replace(state)
        print(f"MD57 {info['version']} listening at http://{host}:{port} (PID {os.getpid()})",flush=True)
        try:
            server.run()
        finally:
            server.close()
            state.unlink(missing_ok=True)


def main(application=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',default=os.environ.get('MD57_HOST','127.0.0.1'))
    parser.add_argument('--port',type=int,default=int(os.environ.get('MD57_PORT','5000')))
    args=parser.parse_args()
    try:
        serve(application=application,host=args.host,port=args.port)
    except RuntimeError as error:
        sys.exit(str(error))


if __name__=='__main__':
    main()
