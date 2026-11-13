"""零拷贝克隆演示:实际对象统计、写隔离核对与容量账。

运行: python -m warehouse.demo [工作目录]
不带参数时在临时目录中运行;每一步都打印真实的对象读写统计,
并用断言核对隔离性与容量口径,任一核对失败都会以非零退出。
"""
import json, sys, tempfile
from pathlib import Path
from .objects import ObjectStore
from .catalog import Catalog
from .tables import Tables

FIELDS={'10':'工具','20':'团队','30':'版本'}

def _rows():
    return [
        {'row_id':'r1','10':'etl','20':'ops','30':1},
        {'row_id':'r2','10':'etl','20':'research','30':1},
        {'row_id':'r3','10':'bi','20':'ops','30':2},
        {'row_id':'r4','10':'bi','20':'ops','30':2},
        {'row_id':'r5','10':'ml','20':'research','30':3},
        {'row_id':'r6','10':'ml','20':'ops','30':3},
    ]

def _blocks(tables,name):
    m=tables._manifest(tables._entry(name)['head'])
    return [b['digest'] for b in m['blocks']]

def _short(digests):
    return sorted(d[:12] for d in digests)

def run(root):
    store=ObjectStore(root/'objects')
    tables=Tables(store,Catalog(root/'tables'),max_rows_per_block=2)
    report={}
    tables.create('main',FIELDS,rows=_rows())
    before=tables.read('main')

    # 1) 零拷贝克隆:只新增目录条目,不写任何数据对象
    store.reset_stats()
    clone_entry=tables.clone('main','trial')
    report['克隆']={'新建对象':store.stats['puts'],'新建字节':store.stats['put_bytes'],
        '共用同一清单':clone_entry['head']==before['head'],
        '源表身份':before['table'],'克隆身份':clone_entry['table']}
    assert store.stats['puts']==0 and clone_entry['head']==before['head']
    assert clone_entry['table']!=before['table']
    cap0=tables.capacity()
    assert cap0['physical_bytes']*2==cap0['logical_bytes']  # 双表共享同一批对象,物理零增长
    report['克隆']['克隆后逻辑合计']=cap0['logical_bytes']
    report['克隆']['克隆后物理字节']=cap0['physical_bytes']
    trial_snapshot=tables.read('trial')

    handle=tables.open('main')  # 读句柄固定在克隆时刻的源表版本

    # 2) 源表推进:改一行、加两行(触发拆块)、删一行、字段改名、加缺省字段
    main_blocks=set(_blocks(tables,'main'))
    store.reset_stats()
    tables.write('main',upserts=[{'row_id':'r2','10':'etl','20':'research','30':99},
                                 {'row_id':'r7','10':'ml','20':'ops','30':4},
                                 {'row_id':'r8','10':'ml','20':'research','30':4}],
                       deletes=['r5'])
    w=dict(store.stats)
    new_blocks=set(_blocks(tables,'main'))
    tables.rename_field('main','20','组别')
    tables.add_field('main','40','状态',default='active')
    report['源表推进']={'读对象':w['gets'],'读字节':w['get_bytes'],
        '写对象':w['puts'],'写字节':w['put_bytes'],
        '复用块':_short(main_blocks&new_blocks),'重写块':_short(new_blocks-main_blocks)}

    # 3) 隔离核对:克隆与读句柄不受源表写入、改名、加字段影响
    main_now=tables.read('main');trial_now=tables.read('trial')
    assert trial_now==trial_snapshot,'源表推进后克隆看到的内容发生变化'
    assert handle.read()==before,'读句柄版本发生漂移'
    ids=[r['row_id'] for r in main_now['rows']]
    assert ids==sorted(ids) and 'r5' not in ids and 'r7' in ids and len(ids)==7
    assert main_now['fields']['20']=='组别' and all(r['40']=='active' for r in main_now['rows'])
    report['隔离核对']={'克隆行内容不变':trial_now['rows']==trial_snapshot['rows'],
        '克隆字段名不变':trial_now['fields'],'句柄停留在':handle.entry['head'][:12],
        '源表已到':main_now['head'][:12]}

    # 4) 克隆首次写入:只生成必要新块,源表不受任何影响
    trial_blocks=set(_blocks(tables,'trial'))
    store.reset_stats()
    tables.write('trial',upserts=[{'row_id':'r9','10':'etl','20':'ops','30':1}])
    w2=dict(store.stats)
    trial_new=set(_blocks(tables,'trial'))
    assert tables.read('main')==main_now,'克隆写入影响了源表'
    report['克隆首次写入']={'读对象':w2['gets'],'写对象':w2['puts'],'写字节':w2['put_bytes'],
        '复用块':_short(trial_blocks&trial_new),'重写块':_short(trial_new-trial_blocks)}

    # 5) 中断重试:预备后放弃(模拟崩溃),重试得到同一新头且对象全部去重
    e1,h1=tables.prepare_write('trial',upserts=[{'row_id':'r0','10':'x','20':'ops','30':1}])
    store.reset_stats()
    e2,h2=tables.prepare_write('trial',upserts=[{'row_id':'r0','10':'x','20':'ops','30':1}])
    assert h1==h2 and store.stats['puts']==0
    report['中断重试']={'重试得到同一新头':h1==h2,'重试新增对象':store.stats['puts'],
        '重试去重命中':store.stats['put_deduped']}
    tables.commit_write('trial',e2,h2)

    # 6) 容量账:逻辑量按表各计共享,物理量全仓只计一次
    cap=tables.capacity()
    assert cap['physical_bytes']<=cap['on_disk_bytes']
    assert cap['shared_bytes']>0  # 两表各自推进后仍共享未触及的块
    assert cap['physical_bytes']<cap['on_disk_bytes']  # 历史版本与孤儿在盘但不计入物理量
    report['容量账']={'各表逻辑字节':{n:t['bytes'] for n,t in cap['tables'].items()},
        '逻辑合计':cap['logical_bytes'],'物理字节':cap['physical_bytes'],
        '共享字节':cap['shared_bytes'],'在盘字节':cap['on_disk_bytes']}

    # 7) 回收资格:句柄保留期与克隆都是根,共享块绝不可收
    gc1=tables.gc_report()
    trial_reachable=tables._walk(tables._entry('trial')['head'])
    assert not (set(gc1['eligible']['digests'])&trial_reachable),'共享块被误判可回收'
    assert not (set(gc1['eligible']['digests'])&tables._walk(handle.entry['head'])),'句柄版本被误判可回收'
    report['回收资格(源表在册,句柄保留中)']={'可收对象':gc1['eligible']['objects'],
        '可收字节':gc1['eligible']['bytes']}

    # 8) 逻辑删源表:对象一个不动,克隆仍可完整查询
    handle.close()
    tables.drop('main')
    gc2=tables.gc_report()
    assert not (set(gc2['eligible']['digests'])&trial_reachable),'删表后共享块被误判可回收'
    final=tables.read('trial')
    assert final['fields']==FIELDS and '40' not in final['fields']
    got={r['row_id']:r for r in final['rows']}
    for r in _rows(): assert got[r['row_id']]==r,'源表删除后克隆行内容变化'
    assert set(got)=={'r1','r2','r3','r4','r5','r6','r9','r0'}
    report['回收资格(删除源表后)']={'可收对象':gc2['eligible']['objects'],
        '可收字节':gc2['eligible']['bytes'],'克隆仍可完整查询':len(final['rows'])}
    return report

def main():
    args=sys.argv[1:]
    if args:
        root=Path(args[0]);root.mkdir(parents=True,exist_ok=True)
    else:
        keep=tempfile.TemporaryDirectory();root=Path(keep.name)
    report=run(root)
    print(json.dumps(report,ensure_ascii=False,indent=2,sort_keys=True))

if __name__=='__main__':main()
