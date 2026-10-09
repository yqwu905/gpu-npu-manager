#!/usr/bin/env python3
"""测试用的假 ssh：-N -L 时在本机做端口转发，否则在本机 bash 中执行远程命令（HOME 取 FAKE_SSH_HOME）。

FAKE_SSH_NO_FORWARD=1 模拟 sshd 设置了 AllowTcpForwarding no：本地端口能连上，但连接随即被关闭。
"""
import os
import socket
import sys
import threading


def pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def forward(spec):
    bind_host, local_port, remote_host, remote_port = spec.split(":")
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((bind_host, int(local_port)))
    server.listen(16)
    while True:
        client, _ = server.accept()
        if os.environ.get("FAKE_SSH_NO_FORWARD"):
            print("channel 2: open failed: administratively prohibited: open failed", file=sys.stderr, flush=True)
            client.close()
            continue
        try:
            upstream = socket.create_connection((remote_host, int(remote_port)))
        except OSError as exc:
            print("channel open failed: {}".format(exc), file=sys.stderr, flush=True)
            client.close()
            continue
        threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
        threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()


args = sys.argv[1:]
if os.environ.get("FAKE_SSH_LOG"):
    with open(os.environ["FAKE_SSH_LOG"], "a") as f:
        f.write(" ".join(args) + "\n")
if "-O" in args:  # 控制主连接，如 -O exit
    sys.exit(0)
if "-N" in args:
    forward(args[args.index("-L") + 1])
else:
    env = dict(os.environ, HOME=os.environ.get("FAKE_SSH_HOME", os.environ["HOME"]))
    os.execvpe("bash", ["bash", "-c", args[-1]], env)
