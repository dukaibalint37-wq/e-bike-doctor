"""Test doubles: fake HTTPS connection for Gemini and minimal fastapi/pydantic stand-ins."""
import io, json, sys, types


class FakeResp:
    def __init__(self, status, body=b"", chunks=None):
        self.status = status
        self._buf = io.BytesIO(body)
        self._chunks = list(chunks or [])

    def read(self, n=-1):
        if self._chunks:
            return b"".join(self._chunks) if n == -1 else self.read1(n)
        return self._buf.read() if n == -1 else self._buf.read(n)

    def read1(self, n=-1):
        if self._chunks:
            return self._chunks.pop(0)
        return self._buf.read(n)


class FakeConn:
    """Scripted connection. script items: FakeResp or Exception to raise on request()."""
    instances = 0

    def __init__(self, script, log):
        FakeConn.instances += 1
        self.script, self.log, self.sock, self._next = script, log, None, None

    def request(self, method, path, body=None, headers=None):
        self.log.append({"path": path, "body": json.loads(body), "headers": headers})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        self._next = item

    def getresponse(self):
        return self._next

    def close(self):
        pass


def ok_json(text, usage=None):
    return FakeResp(200, json.dumps({"candidates": [{"content": {"parts": [{"text": text}]}}], "usageMetadata": usage or {"promptTokenCount": 100, "candidatesTokenCount": 50, "totalTokenCount": 150}}).encode())


def sse_resp(pieces):
    chunks = []
    for i, p in enumerate(pieces):
        ev = {"candidates": [{"content": {"parts": [{"text": p}]}}]}
        if i == len(pieces) - 1:
            ev["usageMetadata"] = {"promptTokenCount": 80, "candidatesTokenCount": 20, "totalTokenCount": 100}
        line = ("data: " + json.dumps(ev) + "\r\n\r\n").encode()
        chunks.extend([line[:7], line[7:]])  # split inside a line to test buffering
    return FakeResp(200, chunks=chunks)


def install_web_stubs():
    """Minimal fastapi/pydantic replacements so main.py logic can run without the real packages."""
    if "fastapi" in sys.modules and getattr(sys.modules["fastapi"], "_is_stub", False):
        return
    fastapi = types.ModuleType("fastapi"); fastapi._is_stub = True
    class HTTPException(Exception):
        def __init__(self, status_code, detail="", headers=None):
            super().__init__(detail); self.status_code, self.detail, self.headers = status_code, detail, headers or {}
    class FastAPI:
        def __init__(self, **kw): pass
        def middleware(self, kind): return lambda f: f
        def get(self, path): return lambda f: f
        def post(self, path): return lambda f: f
    fastapi.FastAPI, fastapi.HTTPException = FastAPI, HTTPException
    fastapi.Header = lambda default=None: default
    fastapi.Request = object
    responses = types.ModuleType("fastapi.responses")
    class JSONResponse:
        def __init__(self, content=None, status_code=200, headers=None):
            self.content, self.status_code, self.headers = content, status_code, headers or {}
    class StreamingResponse:
        def __init__(self, body, media_type=None, headers=None):
            self.body, self.media_type, self.headers = body, media_type, headers or {}
    responses.JSONResponse, responses.StreamingResponse = JSONResponse, StreamingResponse
    pydantic = types.ModuleType("pydantic")
    class BaseModel:
        def __init__(self, **kw):
            for k, v in type(self).__dict__.items():
                if not k.startswith("_") and not callable(v) and not isinstance(v, (staticmethod, classmethod, property)):
                    setattr(self, k, v.default if isinstance(v, _F) else v)
            for k, v in kw.items(): setattr(self, k, v)
        def model_dump(self, exclude=None):
            ex = exclude or set()
            return {k: (v.model_dump() if isinstance(v, BaseModel) else v) for k, v in self.__dict__.items() if k not in ex}
    class _F:
        def __init__(self, default=None, **kw): self.default = default
    pydantic.BaseModel, pydantic.Field = BaseModel, (lambda default=None, **kw: _F(default))
    sys.modules.update({"fastapi": fastapi, "fastapi.responses": responses, "pydantic": pydantic})


class FakeRequest:
    def __init__(self, ip="203.0.113.5"):
        self.headers = {"x-forwarded-for": ip}
        self.client = None
