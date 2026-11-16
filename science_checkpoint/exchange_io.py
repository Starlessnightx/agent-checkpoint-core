"""Fixed bounded archive worker executed after source/target policy checks."""
import sys
from pathlib import Path
from harness.executor import Command
from checkpoint_plugin.contracts import ContractError, decode

PACK = r'''
import os,sys,tarfile,json,hashlib,stat,base64
source,target,maximum=sys.argv[1:];maximum=int(maximum)
class Bounded:
 def __init__(self,stream):self.stream=stream;self.size=0
 def write(self,data):
  if self.size+len(data)>maximum:raise ValueError('archive exceeds exchange capacity')
  self.size+=len(data);return self.stream.write(data)
 def tell(self):return self.size
 def flush(self):self.stream.flush()
def metadata(info):
 rel='' if info.name=='.' else info.name
 path=os.path.join(source,rel)
 s=os.lstat(path)
 if not (stat.S_ISREG(s.st_mode) or stat.S_ISDIR(s.st_mode) or stat.S_ISLNK(s.st_mode)):raise ValueError('special archive member')
 attrs={}
 if hasattr(os,'listxattr') and hasattr(os,'getxattr'):
  try:
   for n in os.listxattr(path,follow_symlinks=False):
    try:attrs[n]=base64.b64encode(os.getxattr(path,n,follow_symlinks=False)).decode()
    except OSError:pass
  except OSError:pass
 info.pax_headers['SCIENCE.metadata']=json.dumps(dict(mode=stat.S_IMODE(s.st_mode),uid=s.st_uid,gid=s.st_gid,mtime_ns=s.st_mtime_ns,xattrs=attrs),sort_keys=True,separators=(',',':'))
 return info
with open(target,'xb') as raw:
 out=Bounded(raw)
 if not os.path.lexists(source):kind='missing'
 elif os.path.isdir(source):
  kind='directory'
  with tarfile.open(fileobj=out,mode='w|',format=tarfile.PAX_FORMAT) as archive:
   archive.add(source,arcname='.',recursive=False,filter=metadata)
   count=1
   for parent,dirs,files in os.walk(source,followlinks=False):
    dirs.sort();files.sort()
    for name in dirs+files:
     count+=1
     if count>100000:raise ValueError('archive node bound exceeded')
     path=os.path.join(parent,name)
     archive.add(path,arcname=os.path.relpath(path,source),recursive=False,filter=metadata)
 elif os.path.isfile(source):
  kind='file'
  with open(source,'rb') as stream:
   while True:
    b=stream.read(1048576)
    if not b:break
    out.write(b)
 else:raise ValueError('unsupported archive source')
 raw.flush();os.fsync(raw.fileno())
h=hashlib.sha256()
with open(target,'rb') as stream:
 while True:
  b=stream.read(1048576)
  if not b:break
  h.update(b)
print(json.dumps(dict(kind=kind,sha256=h.hexdigest(),size=os.path.getsize(target))))
'''


def pack(source, target, source_io, target_io, maximum):
    source = source_io.io.check(Path(source))
    target = target_io.io.check(Path(target), True)
    target_io.directory(target.parent)
    result = source_io.executor.exec(Command.argv((sys.executable, '-I', '-c', PACK,
        str(source), str(target), str(maximum)), source_io.executor.workspace, 120))
    if result.return_code or result.timed_out:
        raise ContractError('bounded exchange archive capture failed: ' + (result.stderr or '')[-800:])
    return decode(result.stdout.encode())
