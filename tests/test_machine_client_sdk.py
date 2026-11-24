import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Exercise the standalone package without installing it into the server workspace.
with patch.object(sys, "path", [str(Path(__file__).resolve().parents[1] / "packages/machine-client/src"), *sys.path]):
    from nexloop_machine import Client, MachineError
    from nexloop_machine.stdio import serve


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name)/"key"
        self.token = "nxm_" + "a"*12 + "_" + "Z"*43
        self.file.write_text(self.token)
        self.file.chmod(0o600)
        self.client = Client("https://synthetic.invalid", self.file)
        self.calls = []
        self.replies = []
        self.client._exchange = self.exchange
        self.intent = "intent-0001"
        self.digest = "d"*64
        self.cap = "object.note"
        self.ready = {"intent_id":self.intent,"prepare_digest":self.digest}

    def exchange(self, method, path, payload):
        self.calls.append((method,path,json.loads(payload) if payload else None))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, tuple):
            return reply
        return 200,None,json.dumps(reply).encode()

    def test_stdio_catalog_and_wire(self):
        caps = {"capabilities":[{"capability":name,"kind":kind,"description":"service description"}
                                for name,kind in [("object.get","read"),(self.cap,"write"),
                                                  ("approval.decide","write"),("nexloop.machine_key.issue","write")]]}
        done = self.view()
        self.replies = [caps,caps,{"object":"fixture"},caps,self.ready,caps,done,{"capabilities":[]}]
        def call(name,args):
            return ("tools/call",{"name":name,"arguments":args})
        frames = [("initialize",{}),("tools/list",{}),call("object.get",{"params":{"id":"obj"}}),
                  call(self.cap+".prepare",{"intent_id":self.intent,"params":{"note":"hello"}}),
                  call(self.cap+".commit",{"intent_id":self.intent,"prepare_digest":self.digest}),
                  call("object.get",{})]
        source = io.StringIO("".join(json.dumps({"jsonrpc":"2.0","id":i,"method":m,"params":p})+"\n"
                                     for i,(m,p) in enumerate(frames)))
        out = io.StringIO()
        serve(self.client,source,out)
        messages = list(map(json.loads,out.getvalue().splitlines()))
        self.assertEqual([t["name"] for t in messages[1]["result"]["tools"]],
                         ["object.get",self.cap+".prepare",self.cap+".commit"])
        self.assertEqual(json.loads(messages[4]["result"]["content"][0]["text"]),done)
        self.assertIn("error",messages[5])
        bodies = {p:b for _,p,b in self.calls}
        self.assertEqual(bodies["read"],{"verb":"object.get","params":{"id":"obj"}})
        self.assertEqual(bodies["commit"],dict(bodies["prepare"],prepare_digest=self.digest))
        self.assertNotIn(self.token,out.getvalue())

    def test_unknown_only_queries(self):
        for failure in (OSError(self.token), {"phase":"unknown"}, (503,None,b""), (200,None,b"broken")):
            self.client = Client("https://synthetic.invalid", self.file)
            self.client._exchange = self.exchange
            self.calls.clear()
            self.replies=[self.ready,failure,self.view(approval_status="waiting_approval")]
            self.client.prepare(self.cap,self.intent)
            self.assertEqual(self.client.commit(self.cap,self.intent,self.digest)["phase"],"pending_approval")
            with self.assertRaises(MachineError):
                self.client.commit(self.cap,self.intent,self.digest)
            with self.assertRaises(MachineError):
                self.client.prepare(self.cap,self.intent)
            self.assertEqual([p for _,p,_ in self.calls],["prepare","commit","intents/"+self.intent])

    def test_secret_and_retry_errors(self):
        for code in (409,429):
            self.replies=[self.ready,(code,"31",self.token.encode())]
            self.client.prepare(self.cap,self.intent)
            with self.assertRaises(MachineError) as caught:
                self.client.commit(self.cap,self.intent,self.digest)
            self.assertEqual(caught.exception.retry_after,31 if code==429 else None)
            self.assertNotIn(self.token,str(caught.exception))
        self.replies=[{"echo":self.token}]
        with self.assertRaises(MachineError):
            self.client.capabilities()
        self.assertNotIn(self.token,repr(self.client))

    def test_file_and_configuration(self):
        for mode in (0o400,0o640,0o644):
            self.file.chmod(mode)
            with self.assertRaises(MachineError):
                Client("https://synthetic.invalid", self.file)
        self.file.chmod(0o600)
        link = self.file.with_name("link")
        link.symlink_to(self.file)
        with self.assertRaises(MachineError):
            Client("https://synthetic.invalid",link)
        with patch.object(os,"geteuid",return_value=self.file.stat().st_uid+1), self.assertRaises(MachineError):
            Client("https://synthetic.invalid",self.file)
        with self.assertRaises(MachineError):
            Client("http://synthetic.invalid",self.file)

    def test_https_transport_and_ca(self):
        with patch("nexloop_machine.client.ssl.create_default_context", spec=True) as tls:
            client = Client("https://synthetic.invalid:8443", self.file, ca_file="fixture-ca")
            tls.assert_called_once_with(cafile="fixture-ca")
        with patch("nexloop_machine.client.http.client.HTTPSConnection") as factory:
            connection = factory.return_value
            response = connection.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{"capabilities":[]}'
            self.assertEqual(client.capabilities(), {"capabilities":[]})
            factory.assert_called_once_with("synthetic.invalid",8443,context=tls.return_value,timeout=15)
            connection.request.assert_called_once_with("GET","/machine/v1/capabilities",body=None,
                headers={"Authorization":"Bearer "+self.token,"Content-Type":"application/json"})
            connection.close.assert_called_once()

    def view(self, phase="pending_approval", **updates):
        value = dict(intent_id=self.intent, capability=self.cap, phase=phase, result_code=None,
                     outcome_id=None, approval_id="00000000-0000-0000-0000-000000000001",
                     expires_at="2026-01-01T01:00:00Z", committed_at=None)
        return dict(value, **updates)

    def test_query_wrong_intent_or_shape(self):
        for bad in (self.view(intent_id="intent-other"), {"intent_id":self.intent,"phase":"committed"},
                    self.view(replayed=1), self.view(extra=True), self.view(result_code=[])):
            self.replies=[bad]
            with self.assertRaises(MachineError):
                self.client.query(self.intent)

    def test_approved_continuation(self):
        waiting = self.view()
        done = self.view("committed",committed_at="2026-01-01T00:01:00Z",outcome_id="a"*64)
        self.replies=[self.ready,waiting,dict(waiting,approval_status="approved"),done]
        self.client.prepare(self.cap,self.intent,note="original")
        self.assertEqual(self.client.commit(self.cap,self.intent,self.digest),waiting)
        self.assertEqual(self.client.commit(self.cap,self.intent,self.digest),done)
        self.assertEqual([p for _,p,_ in self.calls],["prepare","commit","intents/"+self.intent,"commit"])
        self.assertEqual(self.calls[1],self.calls[-1])

    def test_restart_and_unknown_continuation(self):
        waiting = self.view()
        restarted = Client("https://synthetic.invalid", self.file)
        restarted._exchange = self.exchange
        args = (self.cap,self.intent,self.digest,waiting["approval_id"],{"note":"original"})
        for status in ("waiting_approval","cancelled","rejected","expired",None):
            self.replies=[dict(waiting,approval_status=status)]
            self.assertEqual(restarted.resume_pending(*args)["approval_status"],status)
        self.assertTrue(all(method=="GET" for method,_,_ in self.calls))
        self.calls.clear()
        self.replies=[dict(waiting,approval_status="approved"),OSError("lost"),self.view(intent_id="intent-other")]
        with self.assertRaises(MachineError):
            restarted.resume_pending(*args)
        self.assertEqual([p for _,p,_ in self.calls],["intents/"+self.intent,"commit","intents/"+self.intent])
        self.assertEqual(self.calls[1][2],dict(capability=self.cap,intent_id=self.intent,
                                               params=args[-1],prepare_digest=self.digest))
        with self.assertRaises(MachineError):
            restarted.commit(self.cap,self.intent,self.digest)

    def test_reconcile_wrong_capability(self):
        self.replies=[self.ready,OSError("lost"),self.view(capability="other.note")]
        self.client.prepare(self.cap,self.intent)
        with self.assertRaises(MachineError):
            self.client.commit(self.cap,self.intent,self.digest)
        self.assertEqual([p for _,p,_ in self.calls],["prepare","commit","intents/"+self.intent])

    def test_stdio_resume_from_trusted_record(self):
        waiting = self.view()
        tool = {"capability":self.cap,"kind":"write","description":"note"}
        done = self.view("committed",outcome_id="e"*64,committed_at="2026-01-01T00:03:00Z")
        self.replies=[{"capabilities":[tool]},dict(waiting,approval_status="approved"),done]
        args = {"intent_id":self.intent,"prepare_digest":self.digest,
                "resume":{"approval_id":waiting["approval_id"],"params":{"note":"original"}}}
        message = {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":self.cap+".commit","arguments":args}}
        out = io.StringIO()
        serve(self.client,io.StringIO(json.dumps(message)),out)
        self.assertEqual(json.loads(json.loads(out.getvalue())["result"]["content"][0]["text"]),done)
        self.assertEqual([p for _,p,_ in self.calls],["capabilities","intents/"+self.intent,"commit"])
