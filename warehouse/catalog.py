"""本地目录引用的比较更新及持久写入。

锁文件只创建不删除,崩溃后由内核释放文件锁,重试不会被残留锁文件卡住;
引用的切换通过临时文件加原子替换完成,中途断电要么看到旧值要么看到新值。
"""
from pathlib import Path
import os, json, tempfile, fcntl
from contextlib import contextmanager
from .objects import encode

class Catalog:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.file = self.root/'refs.json'

    def read(self):
        return json.loads(self.file.read_bytes()) if self.file.exists() else {}

    @contextmanager
    def locked(self):
        fd = os.open(self.root/'.refs.lock', os.O_CREAT|os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try: fcntl.flock(fd, fcntl.LOCK_UN)
            finally: os.close(fd)

    def _write(self, refs):
        fd, path = tempfile.mkstemp(dir=self.root, prefix='.refs-')
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(encode(refs)); stream.flush(); os.fsync(stream.fileno())
            os.replace(path, self.file)
        finally:
            if os.path.exists(path): os.unlink(path)

    def compare_update(self, name, expected, value):
        with self.locked():
            refs = self.read()
            if refs.get(name) != expected:
                raise ValueError('目录引用已改变')
            refs[name] = value
            self._write(refs)
        return value

    def remove(self, name, expected):
        with self.locked():
            refs = self.read()
            if refs.get(name) != expected:
                raise ValueError('目录引用已改变')
            del refs[name]
            self._write(refs)
        return expected
