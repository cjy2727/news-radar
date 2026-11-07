"""Windows desktop entry point; bundles server, assets and Python in one EXE."""
import ctypes
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
import urllib.request
import webbrowser

import app


def user_data_dir():
    return Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local') / 'NewsRadar'


class InstanceLock:
    def __init__(self, directory):
        self.handle = None
        self.name = 'Local\\NewsRadar-' + hashlib.sha256(str(directory.resolve()).casefold().encode()).hexdigest()[:24]

    def acquire(self):
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel.CreateMutexW(None, False, self.name)
        error = ctypes.get_last_error()
        if not handle:
            raise ctypes.WinError(error)
        self.handle = handle
        if error == 183:  # ERROR_ALREADY_EXISTS
            self.release()
            return False
        return True

    def release(self):
        if self.handle:
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle.restype = ctypes.c_int
            kernel.CloseHandle(self.handle)
            self.handle = None


def read_json(url):
    # Local HTTP must not be sent to a system-configured proxy.
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=2) as response:
        return json.load(response)


def existing_url(directory):
    for _ in range(30):
        try:
            state = json.loads((directory / 'instance.json').read_text(encoding='utf-8'))
            port = int(state['port'])
            if not 1 <= port <= 65535:
                raise ValueError('Invalid local port')
            url = 'http://127.0.0.1:' + str(port)
            if read_json(url + '/api/health').get('application') == 'news-radar':
                return url
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(.2)
    raise RuntimeError('이미 실행 중인 뉴스 레이더가 응답하지 않습니다. 기존 실행 창을 확인해 주세요.')


class Runtime:
    def __init__(self, directory, preferred_port=8080):
        self.directory = directory
        self.preferred_port = preferred_port
        self.lock = InstanceLock(directory)
        self.server = None
        self.server_thread = None
        self.scheduler = None
        self.url = None
        self.stopped = threading.Event()

    def start(self, collect=True):
        self.directory.mkdir(parents=True, exist_ok=True)
        if not self.lock.acquire():
            self.url = existing_url(self.directory)
            return False
        try:
            app.DATA = self.directory
            app.STOP.clear()
            app.init()
            try:
                self.server = app.ThreadingHTTPServer(('127.0.0.1', self.preferred_port), app.Handler)
            except OSError:
                self.server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
            self.url = 'http://127.0.0.1:' + str(self.server.server_port)
            self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.server_thread.start()
            state = self.directory / 'instance.json'
            temporary = state.with_suffix('.tmp')
            temporary.write_text(json.dumps({'port': self.server.server_port, 'pid': os.getpid()}), encoding='utf-8')
            temporary.replace(state)
            if collect:
                self.scheduler = threading.Thread(target=app.schedule, daemon=True)
                self.scheduler.start()
            return True
        except Exception:
            self.stop()
            raise

    def stop(self):
        # A duplicate launcher must not stop the owner.
        if not self.lock.handle:
            return
        try:
            app.STOP.set()
            if self.scheduler:
                self.scheduler.join()
            if self.server:
                if self.server_thread:
                    self.server.shutdown()
                    self.server_thread.join()
                self.server.server_close()
            # Let the in-flight fetch finish and its SQLite transaction close.
            with app.LOCK:
                pass
            (self.directory / 'instance.json').unlink(missing_ok=True)
        finally:
            self.lock.release()
            self.stopped.set()


def make_window(root, runtime):
    root.title('News Radar · 뉴스 레이더')
    root.geometry('470x285')
    root.resizable(False, False)
    root.configure(bg='#f5f7fa')
    frame = ttk.Frame(root, padding=24)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='NEWS RADAR', font=('Segoe UI', 20, 'bold')).pack(anchor='w')
    status = ttk.Label(frame, text='뉴스 페이지가 실행 중입니다.', font=('Malgun Gothic', 11))
    status.pack(anchor='w', pady=(14, 8))
    ttk.Label(frame, text=runtime.url).pack(anchor='w')
    ttk.Label(frame, text='이 창을 닫으면 자동 수집과 서버가 종료됩니다.\n브라우저만 닫으면 수집은 계속됩니다.').pack(anchor='w', pady=14)
    buttons = ttk.Frame(frame)
    buttons.pack(fill='x', pady=(8, 0))

    def open_page():
        try:
            if not webbrowser.open(runtime.url):
                messagebox.showinfo('뉴스 페이지', '브라우저 주소창에 아래 주소를 입력해 주세요.\n' + runtime.url, parent=root)
        except Exception:
            messagebox.showinfo('뉴스 페이지', runtime.url, parent=root)

    ttk.Button(buttons, text='뉴스 페이지 열기', command=open_page).pack(side='left')
    ttk.Button(buttons, text='데이터 폴더', command=lambda: os.startfile(str(runtime.directory))).pack(side='left', padx=8)
    closing = False

    def close():
        nonlocal closing
        if closing:
            return
        closing = True
        for button in buttons.winfo_children():
            button.configure(state='disabled')
        status.configure(text='저장 후 종료 중입니다. 잠시 기다려 주세요…')
        threading.Thread(target=runtime.stop, daemon=True).start()
        def check():
            if runtime.stopped.is_set():
                root.destroy()
            else:
                root.after(100, check)
        check()

    ttk.Button(buttons, text='종료', command=close).pack(side='right')
    root.protocol('WM_DELETE_WINDOW', close)
    return close


def configure_logging(directory):
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(directory / 'server.log', maxBytes=2_000_000, backupCount=2, encoding='utf-8')
    logging.basicConfig(level=logging.INFO, handlers=[handler], format='%(asctime)s %(levelname)s %(message)s')


def self_test(report):
    """Exercise the actual frozen bundle offline, without touching user data."""
    report = Path(report).resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    result = {'ok': False}
    runtime = None
    root = None
    try:
        with tempfile.TemporaryDirectory(dir=report.parent, prefix='news-radar-test-') as folder:
            directory = Path(folder)
            with socket.socket() as occupied:
                occupied.bind(('127.0.0.1', 0))
                occupied.listen()
                runtime = Runtime(directory, occupied.getsockname()[1])
                assert runtime.start(collect=False)
                assert runtime.server.server_port != occupied.getsockname()[1]
                assert read_json(runtime.url + '/api/health')['application'] == 'news-radar'
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for resource in ('/', '/style.css', '/app.js'):
                    with opener.open(runtime.url + resource) as response:
                        assert response.status == 200 and response.read()
                duplicate = Runtime(directory)
                assert not duplicate.start(collect=False)
                assert duplicate.url == runtime.url
                duplicate.stop()
                payload = json.dumps({'name': 'EXE persistence test'}).encode()
                req = urllib.request.Request(runtime.url + '/api/keywords', data=payload, headers={'Content-Type':'application/json', 'X-News-Radar':'1'})
                with opener.open(req) as response:
                    assert response.status == 201
                root = tk.Tk()
                root.withdraw()
                close = make_window(root, runtime)
                root.update()
                close()
                root.mainloop()
                root = None
                assert runtime.stopped.is_set()
                runtime = Runtime(directory, 0)
                assert runtime.start(collect=False)
                assert any(k['name'] == 'EXE persistence test' for k in read_json(runtime.url + '/api/state')['keywords'])
                runtime.stop()
                result = {'ok': True, 'checks': ['bundled HTML/CSS/JS', 'HTTP', 'occupied port fallback', 'duplicate launch', 'Tk window initialization and close', 'SQLite persistence after restart', 'clean shutdown']}
    except Exception:
        import traceback
        result['error'] = traceback.format_exc()
    finally:
        if runtime:
            runtime.stop()
        if root:
            root.destroy()
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if result['ok'] else 1


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--self-test':
        return self_test(sys.argv[2])
    root = tk.Tk()
    root.withdraw()
    runtime = Runtime(user_data_dir())
    try:
        configure_logging(runtime.directory)
        if not runtime.start():
            webbrowser.open(runtime.url)
            return 0
        make_window(root, runtime)
        root.deiconify()
        root.after(300, lambda: webbrowser.open(runtime.url))
        root.mainloop()
        return 0
    except Exception as exc:
        logging.exception('News Radar startup failed')
        messagebox.showerror('뉴스 레이더 실행 오류', str(exc) + '\n\n로그 위치: ' + str(runtime.directory / 'server.log'), parent=root)
        return 1
    finally:
        runtime.stop()
        try:
            root.destroy()
        except tk.TclError:
            pass


if __name__ == '__main__':
    sys.exit(main())
