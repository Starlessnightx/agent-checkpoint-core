import asyncio
from pathlib import Path
import tempfile
import unittest
from checkpoint_plugin.adapters.agent_state import HarnessStateAdapter
from checkpoint_plugin.adapters.agent_import import HarnessStateImporter
from harness.core.runtime import AgentRuntime
from harness.core.turn import TurnLoopDriver
from harness.tasks import RegularTask
from harness.protocol import Shutdown, UserTurn, TurnOutcome
from harness.spec import build_spec
from science_checkpoint.boundary import write_snapshot
from harness.resume import _to_snapshot
from tests.test_turn_loop import Model, call, response
ROOT=Path(__file__).resolve().parents[1]
class LatestCheckpointTest(unittest.IsolatedAsyncioTestCase):
 async def drive(self,s,runner,name):
  loop=s.open_submission_loop(runner);consumer=asyncio.create_task(loop.run())
  try:
   loop.submit(UserTurn(session_id=s.session_id,turn_id=name,operation_id='start-'+name,text=s.task.text))
   return await asyncio.wait_for(loop.wait_result(name),10)
  finally:
   loop.submit(Shutdown(session_id=s.session_id,operation_id='stop-'+name));await asyncio.wait_for(consumer,10)
 async def test_save_restore_two_continuations_without_replay(self):
  with tempfile.TemporaryDirectory() as d:
   base=Path(d);spec=build_spec(ROOT,base/'workspace');control=base/'control';points=[]
   adapter=HarnessStateAdapter(spec,model_exporter=lambda *_:{'kind':'test'})
   class Handler:
    async def at_boundary(self,s,b,c,t):
     if b.kind=='after_tools':points.append(write_snapshot(control,adapter.freeze(s,b,c,t),pack_root=control/'.archive-pack'))
   model=Model([response(call('write_file',{'path':'saved.txt','content':'checkpoint'},'old-write')),response(text='done')])
   runtime=AgentRuntime(spec,model,boundary_factory=lambda _:Handler());s=runtime.create_session('write and inspect')
   s.context_manager.observations.put('fixture',{'retained':True})
   outcome=await self.drive(s,RegularTask(s,TurnLoopDriver(spec,model,runtime.execute_tool_calls)),'source')
   self.assertEqual(outcome.outcome,TurnOutcome.COMPLETED);self.assertEqual(len(points),1)
   source=control/points[0]
   for i in range(2):
    snapshot=base/f'branch{i}'/'snapshot';_to_snapshot(source,snapshot)
    model=Model([response(call('read_file',{'path':'saved.txt'},f'new-{i}')),response(text='continued')])
    runtime=AgentRuntime(spec,model)
    imported=HarnessStateImporter(model_validator=lambda *_:True).restore_snapshot(snapshot,runtime)
    self.assertEqual(imported.session.messages[-1]['role'],'tool')
    self.assertTrue(imported.session.context_manager.observations.fallbacks)
    outcome=await self.drive(imported.session,imported.runner,f'branch{i}')
    self.assertEqual(outcome.outcome,TurnOutcome.COMPLETED);self.assertEqual(len(model.requests),2)
    self.assertEqual([e['name'] for e in imported.session.recorder.events if e['type']=='tool'],['read_file'])
    self.assertEqual((spec.workspace/'saved.txt').read_text(),'checkpoint')
   self.assertTrue(source.exists())
