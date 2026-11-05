"""Dynamic directory scheduling with a bounded metadata queue and one consumer."""
import os
import queue
import threading


def walk_metadata(root, workers=4, capacity=1024, excluded_directory=lambda _:False, file_filter=lambda _:None):
    directories=queue.Queue(); results=queue.Queue(maxsize=capacity); done=threading.Event(); stop=threading.Event()
    directories.put(str(root))
    def emit(*event):
        while not stop.is_set():
            try:results.put(event,timeout=.1);return
            except queue.Full:pass
    def producer():
        while True:
            directory=directories.get()
            try:
                if directory is None:return
                if stop.is_set():continue
                try:
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            if stop.is_set():break
                            try:
                                if entry.is_symlink() or (hasattr(entry,'is_junction') and entry.is_junction()):
                                    emit('link',entry.name);continue
                                if entry.is_dir(follow_symlinks=False):
                                    if excluded_directory(entry.name):emit('excluded_directory',entry.path)
                                    else:directories.put(entry.path)
                                elif entry.is_file(follow_symlinks=False):
                                    reason=file_filter(entry.name)
                                    if reason:emit('excluded_file',reason)
                                    else:emit('file',entry.path,entry.stat(follow_symlinks=False))
                                else:emit('special',entry.path)
                            except OSError as exc:emit('file_error',entry.path,str(exc))
                except Exception as exc:emit('directory_error',directory,str(exc))
            finally:directories.task_done()
    threads=[threading.Thread(target=producer,daemon=True) for _ in range(workers)]
    for thread in threads:thread.start()
    def finish():
        directories.join()
        for _ in threads:directories.put(None)
        for thread in threads:thread.join()
        done.set()
    coordinator=threading.Thread(target=finish,daemon=True);coordinator.start()
    try:
        while not done.is_set() or not results.empty():
            try:yield results.get(timeout=.1)
            except queue.Empty:yield ('idle',)
    finally:
        stop.set()
        coordinator.join()
