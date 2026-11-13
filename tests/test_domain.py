"""领域资料及现有接口核对。"""
import json,tempfile,unittest
from pathlib import Path
from warehouse.objects import ObjectStore
from warehouse.branches import inspect

class DomainTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=ObjectStore(self.root/'objects')
        self.data=json.loads(Path('data/source.json').read_text(encoding='utf-8'))
    def test_existing_reader(self):
        result=inspect(self.data,self.store,self.root)
        self.assertTrue(result)

    def test_ancestry_is_not_arrival_order(self):
        from warehouse.branches import Branches
        b=Branches(self.store,self.root/'refs');a=b.commit([],[],{});x=b.commit([a],[{'row_id':'x'}],{});y=b.commit([a],[{'row_id':'y'}],{})
        self.assertEqual(b.closure(x)&b.closure(y),{a});self.assertNotEqual(x,y)
