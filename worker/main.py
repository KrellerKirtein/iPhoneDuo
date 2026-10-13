import argparse
import json
import os
import sys
import threading
import uuid
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'legacy'))
from protocol import Channel, LogStream, Cancelled


class AccountLock:
    def __init__(self, path):
        self.file = open(path, 'a+b')

    def __enter__(self):
        self.file.seek(0)
        if os.name == 'nt':
            import msvcrt
            self.file.write(b'\0')
            self.file.flush()
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return self

    def __exit__(self, *args):
        self.file.close()


def main():
    # Local removal only: reuse the OS vault without starting a login task.
    if '--forget-account' in sys.argv[1:]:
        parser = argparse.ArgumentParser()
        parser.add_argument('--forget-account', required=True, type=uuid.UUID)
        args = parser.parse_args()
        if args.forget_account.int == 0:
            parser.error('不能移除通用预设。')
        from credentials import Vault
        Vault().delete(str(args.forget_account))
        return
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--account-id', required=True, type=uuid.UUID)
    parser.add_argument('--task-id', required=True, type=uuid.UUID)
    args = parser.parse_args()
    channel = Channel(str(args.account_id), str(args.task_id))
    service = None
    error = None
    try:
        raw = sys.stdin.readline(1_048_577)
        if len(raw) > 1_048_576:
            raise ValueError('任务输入过大。')
        request = json.loads(raw)
        if request.get('account_id') != channel.account_id or request.get('task_id') != channel.task_id:
            raise ValueError('任务与账号标识不匹配。')
        account = request.get('account', '')
        local = args.account_id.int == 0
        if local and request.get('action') not in ('catalog', 'address_read', 'address_save'):
            raise ValueError('此操作需要选择并登录 Apple 账号。')
        if not local and (not isinstance(account, str) or not account.strip()):
            raise ValueError('账号不能为空。')
        data_dir = Path(args.data_dir).resolve()
        root = data_dir / 'settings' if local else data_dir / 'accounts' / channel.account_id
        task_dir = root / 'tasks' / channel.task_id
        task_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        threading.Thread(target=channel.read_replies, args=(sys.stdin,), daemon=True).start()
        with AccountLock(root / '.worker.lock'), redirect_stdout(LogStream(channel)), redirect_stderr(LogStream(channel)):
            from service import Service
            service = Service(channel, root, task_dir, account)
            service.enable_logging()
            result = service.run(request['action'], request.get('params', {}))
            channel.emit('result', result=result, order_submitted=service.submitted)
    except Cancelled:
        channel.emit('cancelled', message='任务已取消。', order_submitted=bool(service and service.submitted))
    except Exception as exc:
        error = exc
        # Never include requests' exception URLs, headers, or a traceback with locals.
        import requests
        message = '网络请求失败，请检查网络或系统代理。' if isinstance(exc, requests.RequestException) else str(exc)
        if not isinstance(exc, (ValueError, OSError)):
            message = f'操作未完成（{type(exc).__name__}），请查看任务阶段。'
        if service and service.submitted:
            message += ' 订单提交结果可能未确认，请查询订单后再决定下一步。'
        channel.emit('error', message=message[:2000], order_submitted=bool(service and service.submitted),
                     account_invalid=bool(service and service.should_remove_account(exc)))
    finally:
        if service:
            try:
                service.cleanup_credentials(error)
            except Exception:
                pass


if __name__ == '__main__':
    # Frozen Windows workers may ignore PYTHONIOENCODING; IPC always uses UTF-8.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='strict')
    main()
