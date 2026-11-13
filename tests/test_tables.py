"""分块克隆表的隔离、行身份、重试与容量账核对。"""
import tempfile, unittest
from pathlib import Path
from warehouse.objects import ObjectStore
from warehouse.catalog import Catalog
from warehouse.tables import Tables

FIELDS={'10':'工具','20':'团队','30':'版本'}

def rows6():
    return [
        {'row_id':'r1','10':'etl','20':'ops','30':1},
        {'row_id':'r2','10':'etl','20':'research','30':1},
        {'row_id':'r3','10':'bi','20':'ops','30':2},
        {'row_id':'r4','10':'bi','20':'ops','30':2},
        {'row_id':'r5','10':'ml','20':'research','30':3},
        {'row_id':'r6','10':'ml','20':'ops','30':3},
    ]

class TableTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.store=ObjectStore(self.root/'objects')
        self.tables=Tables(self.store,Catalog(self.root/'tables'),max_rows_per_block=2)
        self.tables.create('main',FIELDS,rows=rows6())
        self.base=self.tables.read('main')

    def ids(self,name):
        return [r['row_id'] for r in self.tables.read(name)['rows']]

    def blocks(self,name):
        m=self.tables._manifest(self.tables._entry(name)['head'])
        return [b['digest'] for b in m['blocks']]

    # ---- 克隆:零拷贝、独立身份 ----
    def test_clone_zero_copy(self):
        self.store.reset_stats()
        entry=self.tables.clone('main','trial')
        self.assertEqual(self.store.stats['puts'],0)
        self.assertEqual(self.store.stats['put_bytes'],0)
        self.assertEqual(entry['head'],self.base['head'])
        self.assertNotEqual(entry['table'],self.base['table'])
        self.assertEqual(self.tables.read('trial')['rows'],self.base['rows'])

    def test_clone_retry_idempotent(self):
        e1=self.tables.clone('main','trial')
        self.store.reset_stats()
        e2=self.tables.clone('main','trial')
        self.assertEqual(e1,e2)
        self.assertEqual(self.store.stats['puts'],0)
        self.tables.write('main',deletes=['r1'])
        e3=self.tables.clone('main','trial')  # 源表已推进,重试返回既有克隆且不改动它
        self.assertEqual(e3,e1)
        self.assertEqual(self.tables.read('trial')['rows'],self.base['rows'])
        with self.assertRaises(ValueError): self.tables.clone('main','main')

    # ---- 隔离:源表推进不改变克隆,克隆写入不影响源表 ----
    def test_source_advance_clone_untouched(self):
        self.tables.clone('main','trial')
        before=self.tables.read('trial')
        self.tables.write('main',upserts=[{'row_id':'r2','10':'etl','20':'research','30':99},
                                         {'row_id':'r7','10':'ml','20':'ops','30':4},
                                         {'row_id':'r8','10':'ml','20':'research','30':4}],
                               deletes=['r5'])
        self.tables.rename_field('main','20','组别')
        self.tables.add_field('main','40','状态',default='active')
        self.assertEqual(self.tables.read('trial'),before)
        main=self.tables.read('main')
        self.assertEqual(main['fields']['20'],'组别')
        self.assertTrue(all(r['40']=='active' for r in main['rows']))
        self.assertEqual([r['row_id'] for r in main['rows']],
                         ['r1','r2','r3','r4','r6','r7','r8'])
        r2=[r for r in main['rows'] if r['row_id']=='r2'][0]
        self.assertEqual(r2['30'],99)

    def test_clone_write_isolated(self):
        self.tables.clone('main','trial')
        main_blocks=self.blocks('main')
        self.tables.write('trial',upserts=[{'row_id':'r9','10':'etl','20':'ops','30':1}])
        self.assertEqual(self.blocks('main'),main_blocks)
        self.assertEqual(self.tables.read('main'),self.base)
        trial=self.tables.read('trial')
        self.assertEqual(len(trial['rows']),7)
        self.assertIn('r9',[r['row_id'] for r in trial['rows']])

    def test_shared_blocks_reused_not_rewritten(self):
        self.tables.clone('main','trial')
        shared=set(self.blocks('trial'))
        self.store.reset_stats()
        self.tables.write('trial',upserts=[{'row_id':'r2','10':'etl','20':'research','30':7}])
        stats=dict(self.store.stats)
        now=set(self.blocks('trial'))
        self.assertEqual(len(shared&now),2)      # 未触及的两块继续以摘要复用
        self.assertEqual(stats['puts'],2)        # 只写 1 个新块 + 1 份新清单
        self.assertEqual(self.tables.read('main'),self.base)

    # ---- 行身份:拆块、删除、缺省 ----
    def test_block_split_preserves_identity(self):
        original={r['row_id']:r for r in self.base['rows']}
        self.tables.write('main',upserts=[{'row_id':'n%d'%i,'10':'x','20':'y','30':i} for i in range(5)])
        got=self.tables.read('main')['rows']
        ids=[r['row_id'] for r in got]
        self.assertEqual(ids,sorted(ids))
        self.assertEqual(len(ids),len(set(ids)))
        for r in got:
            if r['row_id'] in original: self.assertEqual(r,original[r['row_id']])
        m=self.tables._manifest(self.tables._entry('main')['head'])
        self.assertEqual(m['rows'],11)
        self.assertTrue(all(b['rows']<=2 for b in m['blocks']))
        self.assertGreater(len(m['blocks']),3)   # 确实发生了拆块

    def test_delete_preserves_identity(self):
        self.tables.write('main',deletes=['r2','r5','不存在'])
        got=self.tables.read('main')['rows']
        self.assertEqual([r['row_id'] for r in got],['r1','r3','r4','r6'])
        original={r['row_id']:r for r in self.base['rows']}
        for r in got: self.assertEqual(r,original[r['row_id']])
        m=self.tables._manifest(self.tables._entry('main')['head'])
        self.assertEqual(m['rows'],4)
        self.assertEqual(sum(b['rows'] for b in m['blocks']),4)

    def test_delete_drops_empty_block(self):
        self.tables.write('main',deletes=['r1','r2'])
        m=self.tables._manifest(self.tables._entry('main')['head'])
        self.assertEqual([(b['first'],b['last']) for b in m['blocks']],[('r3','r4'),('r5','r6')])

    def test_defaults_and_explicit_null(self):
        self.tables.add_field('main','40','状态',default='新')
        got={r['row_id']:r for r in self.tables.read('main')['rows']}
        self.assertTrue(all(r['40']=='新' for r in got.values()))
        self.tables.write('main',upserts=[
            {'row_id':'r1','10':'etl','20':'ops','30':1,'40':None},
            {'row_id':'r7','10':'x','20':'y','30':1,'40':'忙'}])
        got={r['row_id']:r for r in self.tables.read('main')['rows']}
        self.assertIsNone(got['r1']['40'])       # null 是显式值,不被缺省覆盖
        self.assertEqual(got['r7']['40'],'忙')
        self.assertEqual(got['r2']['40'],'新')
        self.assertEqual(sorted(got),['r1','r2','r3','r4','r5','r6','r7'])

    def test_schema_evolution_independent(self):
        self.tables.clone('main','trial')
        self.tables.rename_field('main','20','组别')
        self.tables.add_field('trial','40','阶段',default='p0')
        main=self.tables.read('main');trial=self.tables.read('trial')
        self.assertEqual(main['fields']['20'],'组别')
        self.assertEqual(trial['fields']['20'],'团队')
        self.assertNotIn('40',main['fields'])
        self.assertEqual(trial['defaults'],{'40':'p0'})
        self.assertTrue(all(r['40']=='p0' for r in trial['rows']))
        with self.assertRaises(ValueError): self.tables.rename_field('main','99','x')
        with self.assertRaises(ValueError): self.tables.add_field('main','10','重复')

    # ---- 写入校验 ----
    def test_write_validation(self):
        with self.assertRaises(ValueError): self.tables.write('main',upserts=[{'row_id':'x','99':'?'}])
        with self.assertRaises(ValueError): self.tables.write('main',upserts=[{'row_id':'x','10':'a'},{'row_id':'x','10':'b'}])
        with self.assertRaises(ValueError): self.tables.write('main',upserts=[{'row_id':'x','10':'a'}],deletes=['x'])
        with self.assertRaises(ValueError): self.tables.write('main',upserts=[{'10':'a'}])
        self.assertEqual(self.tables.read('main'),self.base)

    def test_empty_table_first_write(self):
        self.tables.create('empty',FIELDS)
        self.assertEqual(self.tables.read('empty')['rows'],[])
        self.tables.write('empty',upserts=[{'row_id':'a','10':'x','20':'y','30':1}])
        self.assertEqual(self.ids('empty'),['a'])

    def test_missing_table(self):
        with self.assertRaises(ValueError): self.tables.read('没有')
        with self.assertRaises(ValueError): self.tables.drop('没有')
        with self.assertRaises(ValueError): self.tables.clone('没有','x')

    # ---- 读句柄:各自版本稳定,且为容量账的根 ----
    def test_read_handle_stable(self):
        handle=self.tables.open('main')
        self.tables.write('main',upserts=[{'row_id':'r1','10':'改','20':'ops','30':9}])
        self.assertEqual(handle.read(),self.base)
        self.assertNotEqual(self.tables.read('main'),self.base)
        handle.close()
        with self.assertRaises(ValueError): handle.read()

    def test_handles_on_both_sides(self):
        self.tables.clone('main','trial')
        h1=self.tables.open('main');h2=self.tables.open('trial')
        self.tables.write('main',deletes=['r1'])
        self.tables.write('trial',deletes=['r6'])
        self.assertEqual(h1.read()['rows'],self.base['rows'])
        self.assertEqual(h2.read()['rows'],self.base['rows'])
        h1.close();h2.close()

    # ---- 中断与重试 ----
    def test_interrupted_write_retryable(self):
        head_before=self.tables._entry('main')['head']
        entry,new_head=self.tables.prepare_write('main',upserts=[{'row_id':'r7','10':'x','20':'y','30':1}])
        # 模拟崩溃:对象已写,目录未切换,读到的仍是旧版本
        self.assertEqual(self.tables._entry('main')['head'],head_before)
        self.assertEqual(self.ids('main'),['r1','r2','r3','r4','r5','r6'])
        entry2,new_head2=self.tables.prepare_write('main',upserts=[{'row_id':'r7','10':'x','20':'y','30':1}])
        self.assertEqual(new_head,new_head2)     # 确定性内容,重试幂等
        self.tables.commit_write('main',entry2,new_head2)
        self.assertIn('r7',self.ids('main'))
        with self.assertRaises(ValueError):
            self.tables.commit_write('main',entry2,new_head2)  # 过期期望被拒绝

    def test_write_retries_transient_conflict(self):
        original=self.tables.catalog.compare_update
        state={'calls':0}
        def flaky(name,expected,value):
            state['calls']+=1
            if state['calls']==1: raise ValueError('目录引用已改变')
            return original(name,expected,value)
        self.tables.catalog.compare_update=flaky
        try:
            self.tables.write('main',upserts=[{'row_id':'r7','10':'x','20':'y','30':1}])
        finally:
            self.tables.catalog.compare_update=original
        self.assertGreaterEqual(state['calls'],2)
        self.assertIn('r7',self.ids('main'))

    def test_stale_lock_file_does_not_block_retry(self):
        c=Catalog(self.root/'cat2')
        (self.root/'cat2'/'.refs.lock').write_bytes(b'')  # 崩溃残留的锁文件
        c.compare_update('a',None,'v')
        self.assertEqual(c.read(),{'a':'v'})

    def test_create_retry_idempotent(self):
        a=self.tables.create('t',FIELDS,rows=rows6())
        b=self.tables.create('t',FIELDS,rows=rows6())
        self.assertEqual(a,b)
        with self.assertRaises(ValueError): self.tables.create('t',{'9':'别的'})

    # ---- 容量账与回收资格 ----
    def test_capacity_accounting(self):
        self.tables.clone('main','trial')
        cap=self.tables.capacity()
        lm=cap['tables']['main']['bytes'];lt=cap['tables']['trial']['bytes']
        self.assertEqual(lm,lt)
        self.assertEqual(cap['logical_bytes'],lm+lt)   # 共享块在每个表各计一次
        self.assertEqual(cap['physical_bytes'],lm)     # 物理上只计一次
        self.assertEqual(cap['shared_bytes'],lm)
        self.assertEqual(cap['physical_bytes'],cap['on_disk_bytes'])
        self.tables.write('main',upserts=[{'row_id':'r2','10':'etl','20':'research','30':7}])
        cap2=self.tables.capacity()
        self.assertGreater(cap2['physical_bytes'],cap['physical_bytes'])
        self.assertLess(cap2['physical_bytes'],cap2['logical_bytes'])
        self.assertGreater(cap2['shared_bytes'],0)

    def test_gc_report_respects_roots(self):
        self.tables.clone('main','trial')
        handle=self.tables.open('main')
        v1=handle.entry['head']
        self.tables.write('main',upserts=[{'row_id':'r2','10':'etl','20':'research','30':7}])
        v2=self.tables._entry('main')['head']
        self.tables.prepare_write('main',upserts=[{'row_id':'r3','10':'x','20':'y','30':0}])  # 中断留下孤儿
        trial_set=self.tables._walk(self.tables._entry('trial')['head'])
        gc=self.tables.gc_report()
        eligible=set(gc['eligible']['digests'])
        self.assertEqual(gc['eligible']['objects'],2)  # 孤儿块与孤儿清单
        self.assertFalse(eligible&trial_set)           # 共享块不可收
        self.assertFalse(eligible&self.tables._walk(v1))  # 句柄保留的版本不可收
        self.assertNotIn(v2,eligible)                  # 在册表头不可收
        handle.close()
        self.tables.drop('main')
        gc2=self.tables.gc_report()
        eligible2=set(gc2['eligible']['digests'])
        self.assertFalse(eligible2&trial_set)          # 逻辑删表后共享块仍不可收
        self.assertIn(v2,eligible2)                    # 源表独占的清单现在可收
        self.assertEqual(self.tables.read('trial')['rows'],self.base['rows'])

    def test_drop_does_not_delete_objects(self):
        self.tables.clone('main','trial')
        before=self.store.list_objects()
        self.tables.drop('main')
        self.assertEqual(self.store.list_objects(),before)  # 逻辑删表不动任何对象
        gc=self.tables.gc_report()
        trial_set=self.tables._walk(self.tables._entry('trial')['head'])
        self.assertFalse(set(gc['eligible']['digests'])&trial_set)

    def test_expired_pin_not_a_root(self):
        handle=self.tables.open('main',retention_s=0.0)
        gc=self.tables.gc_report()
        self.assertFalse(any(r['kind']=='pin' for r in gc['roots']))
        handle.close()

    def test_pin_counts_in_physical(self):
        handle=self.tables.open('main')
        self.tables.write('main',deletes=['r1','r2','r3','r4','r5','r6'])
        cap=self.tables.capacity()
        self.assertEqual(cap['physical_bytes'],cap['on_disk_bytes'])  # 旧版本被句柄保住
        handle.close()
        gc=self.tables.gc_report()
        self.assertGreater(gc['eligible']['objects'],0)

    # ---- 完整性 ----
    def test_tampered_block_detected(self):
        digest=self.blocks('main')[0]
        (self.store.root/digest).write_bytes(b'{"format":1,"rows":[]}')
        with self.assertRaises(ValueError): self.tables.read('main')
