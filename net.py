
import argparse
import asyncio
import json
import queue
import random
import socket
import struct
import threading
import time

PORT = 7777
START_DELAY = 3.0  # seconds between the host pressing Start and the song starting
CODE_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # no I or O, they look like 1 and 0


def pack(head, data=b""):
    if data:
        head = dict(head, size=len(data))
    raw = json.dumps(head).encode()
    return struct.pack(">I", len(raw)) + raw + data


# server
class Player:
    def __init__(self, pid, writer):
        self.id, self.writer = pid, writer
        self.name, self.songs, self.room, self.char = f"Player {pid}", [], None, None


class Room:
    def __init__(self, code, song, host):
        self.code, self.song, self.host = code, song, host
        self.players = {}
        self.state = "lobby"   # lobby > playing > lobby
        self.uploaded = set()


class Server:
    def __init__(self):
        self.rooms = {}
        self.next_id = 1

    async def handle(self, reader, writer):
        p = Player(self.next_id, writer)
        self.next_id += 1
        try:
            while True:
                size = struct.unpack(">I", await reader.readexactly(4))[0]
                head = json.loads(await reader.readexactly(size))
                data = await reader.readexactly(head["size"]) if head.get("size") else b""
                await self.dispatch(p, head, data)
        except (asyncio.IncompleteReadError, ConnectionError, json.JSONDecodeError):
            pass
        finally:
            await self.leave(p)
            writer.close()

    async def send(self, p, head, data=b""):
        try:
            p.writer.write(pack(head, data))
            await p.writer.drain()
        except ConnectionError:
            pass

    async def error(self, p, msg):
        await self.send(p, {"type": "error", "msg": msg})

    async def broadcast(self, room):
        state = {"type": "room", "code": room.code, "song": room.song, "host": room.host, "state": room.state,
                 "players": [{"id": q.id, "name": q.name, "char": q.char, "uploaded": q.id in room.uploaded}
                             for q in room.players.values()]}
        for q in list(room.players.values()):
            await self.send(q, state)

    async def dispatch(self, p, head, data):
        kind = head.get("type")
        room = p.room
        if kind == "hello":
            p.name = str(head.get("name") or p.name)[:24]
            p.songs = list(head.get("songs", []))
            await self.send(p, {"type": "welcome", "id": p.id})
        elif kind == "ping":
            await self.send(p, {"type": "pong", "t": head.get("t")})
        elif kind == "create":
            await self.leave(p)
            code = "".join(random.choice(CODE_LETTERS) for _ in range(4))
            while code in self.rooms:
                code = "".join(random.choice(CODE_LETTERS) for _ in range(4))
            room = self.rooms[code] = Room(code, head.get("song"), p.id)
            room.players[p.id], p.room, p.char = p, room, None
            await self.broadcast(room)
        elif kind == "join":
            target = self.rooms.get(str(head.get("code", "")).strip().upper())
            if not target:
                return await self.error(p, "No room with that code.")
            if target.state != "lobby":
                return await self.error(p, "That room is in the middle of a song. Try again after it.")
            if target.song not in p.songs:
                return await self.error(p, f"You don't have the song \"{target.song}\" in your maps folder.")
            await self.leave(p)
            target.players[p.id], p.room, p.char = p, target, None
            await self.broadcast(target)
        elif not room:
            await self.error(p, "You're not in a room.")
        elif kind == "pick":
            char = head.get("char")
            if char and any(q.char == char and q is not p for q in room.players.values()):
                return await self.error(p, "Someone else already picked that character.")
            p.char = char
            await self.broadcast(room)
        elif kind == "song" and p.id == room.host and room.state == "lobby":
            missing = [q.name for q in room.players.values() if head.get("song") not in q.songs]
            if missing:
                return await self.error(p, f"{', '.join(missing)} don't have that song.")
            room.song = head.get("song")
            for q in room.players.values():
                q.char = None
            await self.broadcast(room)
        elif kind == "start" and p.id == room.host:
            if room.state != "lobby":
                return
            if any(q.char is None for q in room.players.values()):
                return await self.error(p, "Everyone needs to pick a character first.")
            room.state, room.uploaded = "playing", set()
            for q in room.players.values():
                await self.send(q, {"type": "start", "delay": START_DELAY})
            await self.broadcast(room)
        elif kind == "upload":
            room.uploaded.add(p.id)
            for q in room.players.values():
                if q is not p:
                    await self.send(q, dict(head, type="voice", **{"from": p.id, "name": p.name, "char": p.char}),
                                    data)
            if room.state == "playing" and room.uploaded >= set(room.players):
                room.state = "lobby"
            await self.broadcast(room)
        elif kind == "leave":
            await self.leave(p)

    async def leave(self, p):
        room = p.room
        if not room:
            return
        room.players.pop(p.id, None)
        room.uploaded.discard(p.id)
        p.room, p.char = None, None
        if not room.players:
            self.rooms.pop(room.code, None)
            return
        if room.host == p.id:
            room.host = next(iter(room.players))
        if room.state == "playing" and room.uploaded >= set(room.players):
            room.state = "lobby"
        await self.broadcast(room)


async def serve(port=PORT, ready=None):
    server = await asyncio.start_server(Server().handle, "0.0.0.0", port)
    if ready:
        ready()
    async with server:
        await server.serve_forever()


def start_in_background(port=PORT):
    """Run the server on a daemon thread. Raises OSError if the port can't be opened."""
    started, failure = threading.Event(), []

    def run():
        try:
            asyncio.run(serve(port, started.set))
        except OSError as e:
            failure.append(e)
            started.set()

    threading.Thread(target=run, daemon=True).start()
    started.wait(5)
    if failure:
        raise failure[0]


def local_ip():
    """This machine's address on the local network (no traffic is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


# client

class Client:
    """Talks to a server from the app. Incoming messages land in `inbox` as (head, data)."""

    def __init__(self, address, name, songs):
        host, _, port = address.strip().rpartition(":")
        if not host:
            host, port = address.strip(), PORT
        self.sock = socket.create_connection((host, int(port)), timeout=6)
        self.sock.settimeout(None)
        self.inbox = queue.Queue()
        self.lock = threading.Lock()
        self.rtt = 0.0
        self.id = None
        self.closed = False
        threading.Thread(target=self._reader, daemon=True).start()
        self.send({"type": "hello", "name": name, "songs": songs})
        self.send({"type": "ping", "t": time.time()})

    def send(self, head, data=b""):
        with self.lock:
            self.sock.sendall(pack(head, data))

    def _recv(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("server closed the connection")
            buf += chunk
        return bytes(buf)

    def _reader(self):
        try:
            while True:
                head = json.loads(self._recv(struct.unpack(">I", self._recv(4))[0]))
                data = self._recv(head["size"]) if head.get("size") else b""
                if head.get("type") == "pong":
                    self.rtt = time.time() - head["t"]
                    continue
                if head.get("type") == "welcome":
                    self.id = head["id"]
                self.inbox.put((head, data))
        except (OSError, ConnectionError, ValueError):
            if not self.closed:
                self.inbox.put(({"type": "disconnected"}, b""))

    def close(self):
        self.closed = True
        try:
            self.send({"type": "leave"})
        except OSError:
            pass
        self.sock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Song Voicer room server")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    print(f"Song Voicer server on port {args.port}. Friends join with {local_ip()}:{args.port} "
          f"(or your public address if you've forwarded the port).")
    asyncio.run(serve(args.port))
