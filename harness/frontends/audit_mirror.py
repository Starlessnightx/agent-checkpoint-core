"""Read-only, bounded-demand projection of a live Docker audit session."""
from pathlib import Path
import json
import re
from threading import Event, Thread

from harness.executor import Command, LocalExecutor

# Only the compact index is synchronized. Original events and audit objects
# remain in the container and are read on demand, never rewritten or removed.
_REMOTE_TIMELINE = r'''import json,pathlib,re,sys
p=pathlib.Path(sys.argv[1])/"trajectory.jsonl"
interesting=re.compile(rb'"type"\s*:\s*"(?:api_request|api_response|api_error|run_start|run_end|usage_recorded)"')
with p.open("rb") as f:
 for line in f:
  if not interesting.search(line):continue
  try:e=json.loads(line)
  except (ValueError,UnicodeDecodeError):continue
  if not isinstance(e,dict):continue
  t=e.get("type")
  if t in ("api_request","api_response","api_error"):
   keep={"type","event_id","time","request_id","sampling_index","attempt","provider_model","duration_seconds","payload_ref"}
  elif t in ("run_start","run_end","usage_recorded"):
   keep={"type","event_id","time","sampling_index","task","termination","task_usage","request_usage","request_cost_usd"}
  else:continue
  print(json.dumps({k:v for k,v in e.items() if k in keep},ensure_ascii=False))
stat=p.stat();print(json.dumps({"type":"mirror_cursor","event_id":str(stat.st_size)+":"+str(stat.st_mtime_ns)},ensure_ascii=False))'''

# One requested sample only. Verify content-addressed objects before decoding.
# For the normal step view, skip the large cumulative request body entirely.
_REMOTE_DETAIL = r'''import gzip,hashlib,json,pathlib,re,sys
root=pathlib.Path(sys.argv[1]);rid=sys.argv[2];full=sys.argv[3]=="full"
request=response=None;tools=[];sample=None;offset=None
path=root/"trajectory.jsonl"
# Search from the tail so a long live run reaches its newest request without
# parsing every earlier tool output. Offsets refer to the original byte stream.
def reverse_lines(path):
 with path.open("rb") as f:
  f.seek(0,2);pos=f.tell();tail=b""
  while pos:
   size=min(pos,65536);pos-=size;f.seek(pos);data=f.read(size)+tail;parts=data.split(b"\n");tail=parts[0];end=pos+len(data)
   for part in reversed(parts[1:]):
    start=end-len(part);yield start,part;end=start-1
  if tail:yield 0,tail
for start,line in reverse_lines(path):
 try:e=json.loads(line)
 except (ValueError,UnicodeDecodeError):continue
 if isinstance(e,dict) and e.get("type")=="api_request" and e.get("request_id")==rid:
  request=e;sample=e.get("sampling_index");offset=start;break
if request is None:raise KeyError("unknown request")
with path.open("rb") as f:
 f.seek(offset)
 for line in f:
  try:e=json.loads(line)
  except (ValueError,UnicodeDecodeError):continue
  if not isinstance(e,dict):continue
  if e.get("type")=="api_request" and e.get("request_id")!=rid and e.get("sampling_index")!=sample:break
  if e.get("request_id")==rid and e.get("type") in ("api_response","api_error"):response=e
  if e.get("sampling_index")==sample and e.get("type") in ("tool","permission_decision","tool_started","tool_finished","tool_failed"):tools.append(e)
def body(event,select=False):
 if event is None:return None
 ref=event.get("payload_ref","")
 if not re.fullmatch(r"api/objects/[0-9a-f]{64}\.json\.gz",ref):raise ValueError("invalid audit reference")
 def read(d):
  if not re.fullmatch(r"[0-9a-f]{64}",d):raise ValueError("invalid digest")
  raw=gzip.decompress((root/"api"/"objects"/(d+".json.gz")).read_bytes())
  if hashlib.sha256(raw).hexdigest()!=d:raise ValueError("audit object hash mismatch")
  return json.loads(raw)
 manifest=read(pathlib.Path(ref).name[:-8])
 if manifest.get("version")!=1:raise ValueError("unsupported audit format")
 if manifest.get("kind")=="value":return read(manifest["value"])
 parts=manifest["parts"]
 if select:parts={k:v for k,v in parts.items() if k in ("choices","output","error")}
 return {k:[read(d) for d in v["items"]] if "items" in v else read(v["value"]) for k,v in parts.items()}
payload=body(response,not full)
print(json.dumps({"request_event":request if full else None,"request":body(request) if full else None,"response_event":response if full else None,"response":payload,"tools":tools},ensure_ascii=False,separators=(",",":")))'''


class AuditMirror:
    def __init__(self, container: str, remote: str, trajectory: Path):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", container):
            raise ValueError("invalid container identity")
        source = Path(remote)
        if not source.is_absolute() or ".." in source.parts or ".harness" not in source.parts:
            raise ValueError("remote session must be an absolute .harness audit directory")
        self.container, self.remote, self.trajectory = container, source, trajectory.resolve()
        self.executor = LocalExecutor(self.trajectory.parent)
        self.stop = Event()
        self.state = {"status": "starting", "interval_seconds": 5}
        self.thread = Thread(target=self._run, daemon=True, name="read-only-audit-mirror")

    def _remote(self, script, *args):
        result = self.executor.exec(Command.argv(
            ["docker", "exec", self.container, "python3", "-c", script, str(self.remote), *args],
            self.trajectory.parent, 90))
        if result.return_code or result.timed_out:
            raise OSError("remote audit is not available")
        return result.stdout

    def sync(self):
        try:
            raw = self._remote(_REMOTE_TIMELINE).encode("utf-8")
            self.executor.write_file(self.trajectory, raw)
        except (OSError, ValueError):
            self.state = {"status": "waiting", "message": "轨迹时间线尚未同步，保留已有本地数据"}
            return
        self.state = {"status": "synced", "interval_seconds": 5}

    def read_detail(self, request_id: str, *, full: bool = False):
        value = json.loads(self._remote(_REMOTE_DETAIL, request_id, "full" if full else "step"))
        if not isinstance(value, dict):
            raise ValueError("invalid remote audit response")
        return value

    def _run(self):
        while not self.stop.is_set():
            try:
                self.sync()
            except Exception as exc:
                self.state = {"status": "error", "message": type(exc).__name__}
            self.stop.wait(5)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)
