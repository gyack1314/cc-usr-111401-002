"""分块写时复制表、零拷贝克隆、读句柄与容量账。

对象布局(全部不可变、按内容寻址):
  字段目录 {'format':1,'fields':[{'id','name'}...],'defaults':{字段ID:缺省值}}
  数据块   {'format':1,'rows':[行,...]}        行按 row_id 升序且唯一
  清单     {'format':2,'schema':<目录摘要>,'blocks':[{'digest','first','last','rows'}...],'rows':总行数}
目录条目   {'table':<表身份>,'head':<清单摘要>[,'origin':{'table','head'}]}

清单不含表身份,因此克隆与源表可以共用同一清单对象:克隆只新增一条目录
条目,不写入任何数据对象。任一方写入都按写时复制生成新块与新清单,共享
块绝不被原地修改。行只随 row_id 在块间重新分布,拆块、行删除与字段缺省
都不改变逻辑行身份;缺省仅在读取时物化到物理缺席的字段上。

容量按对象实际字节计:逻辑量是单表可达对象集合的字节和(共享块在每个表
各计一次),物理量是所有有效根(各表目录头与保留期内的读句柄)可达对象
并集的字节和(共享只计一次)。回收资格只报告,不实际清除对象。
"""
import bisect, hashlib, time, uuid
from pathlib import Path
from .objects import encode
from .catalog import Catalog

ROW_ID='row_id'
_MISSING=object()

def _table_id(name):
    return 'tbl-'+hashlib.sha256(encode(['table',name])).hexdigest()[:16]

def _check_field_id(fid):
    if not isinstance(fid,str) or not fid or fid==ROW_ID: raise ValueError('字段ID非法')

class Tables:
    def __init__(self, store, catalog, max_rows_per_block=128, pin_retention_s=3600.0):
        if max_rows_per_block<1: raise ValueError('块行数上限非法')
        self.store=store;self.catalog=catalog
        self.pins=Catalog(Path(catalog.root)/'pins')
        self.max_rows=max_rows_per_block;self.pin_retention=pin_retention_s

    # ---- 清单、字段目录与块的装载校验 ----
    def _manifest(self, head):
        m=self.store.json(head)
        if m.get('format')!=2: raise ValueError('清单格式不支持')
        prev=None;total=0
        for b in m['blocks']:
            if b['rows']<1 or b['first']>b['last']: raise ValueError('块索引非法')
            if prev is not None and prev>=b['first']: raise ValueError('块区间重叠或无序')
            prev=b['last'];total+=b['rows']
        if total!=m['rows']: raise ValueError('清单行数不符')
        return m

    def _schema(self, digest):
        s=self.store.json(digest)
        if s.get('format')!=1: raise ValueError('字段目录格式不支持')
        ids=[f['id'] for f in s['fields']]
        if len(set(ids))!=len(ids): raise ValueError('字段ID重复')
        s.setdefault('defaults',{})
        return s

    def _validate_block(self, entry, block):
        if block.get('format')!=1: raise ValueError('块格式不支持')
        rows=block['rows'];ids=[r.get(ROW_ID) for r in rows]
        if len(rows)!=entry['rows'] or (rows and (ids[0]!=entry['first'] or ids[-1]!=entry['last'])):
            raise ValueError('块与索引不符')
        if ids!=sorted(ids) or len(set(ids))!=len(ids): raise ValueError('块内行身份非法')

    def _walk(self, head):
        m=self._manifest(head)
        return {head,m['schema']}|{b['digest'] for b in m['blocks']}

    def _entry(self, name):
        entry=self.catalog.read().get(name)
        if entry is None: raise ValueError('表不存在: %s'%name)
        return entry

    # ---- 表生命周期:创建、克隆、逻辑删除 ----
    def _commit_new(self, name, entry, compatible):
        try:
            self.catalog.compare_update(name,None,entry)
            return entry
        except ValueError:
            existing=self.catalog.read().get(name)
            if existing is not None and compatible(existing): return existing
            raise ValueError('目标表已存在且身份不符: %s'%name)

    def create(self, name, fields, defaults=None, rows=()):
        if not isinstance(name,str) or not name: raise ValueError('表名非法')
        defaults=dict(defaults or {})
        for fid in fields: _check_field_id(fid)
        if set(defaults)-set(fields): raise ValueError('缺省值字段不在目录中')
        schema={'format':1,'fields':[{'id':f,'name':fields[f]} for f in sorted(fields)],
                'defaults':{f:defaults[f] for f in sorted(defaults)}}
        schema_digest=self.store.put_json(schema)
        empty={'format':2,'schema':schema_digest,'blocks':[],'rows':0}
        rows=list(rows)
        head=self._apply(empty,rows,[]) if rows else self.store.put_json(empty)
        entry={'table':_table_id(name),'head':head}
        return self._commit_new(name,entry,lambda e:e==entry)

    def clone(self, src, dst):
        if not isinstance(dst,str) or not dst: raise ValueError('表名非法')
        src_entry=self._entry(src)
        entry={'table':_table_id(dst),'head':src_entry['head'],
               'origin':{'table':src_entry['table'],'head':src_entry['head']}}
        return self._commit_new(dst,entry,
            lambda e:e.get('table')==entry['table'] and e.get('origin',{}).get('table')==src_entry['table'])

    def drop(self, name):
        entry=self._entry(name)
        self.catalog.remove(name,entry)
        return entry

    # ---- 读取:读句柄固定版本,缺省在物化时补齐 ----
    def open(self, name, retention_s=None):
        entry=self._entry(name)
        pin_id='pin-'+uuid.uuid4().hex
        value={'table':name,'head':entry['head'],
               'expires':time.time()+(self.pin_retention if retention_s is None else retention_s)}
        self.pins.compare_update(pin_id,None,value)
        return ReadHandle(self,name,entry,pin_id,value)

    def read(self, source):
        entry=source.entry if isinstance(source,ReadHandle) else self._entry(source)
        return self._read_entry(entry)

    def _read_entry(self, entry):
        manifest=self._manifest(entry['head'])
        schema=self._schema(manifest['schema'])
        defaults=schema['defaults'];rows=[]
        for b in manifest['blocks']:
            block=self.store.json(b['digest'])
            self._validate_block(b,block)
            for row in block['rows']:
                row=dict(row)
                for fid,val in defaults.items():
                    if fid not in row: row[fid]=val
                rows.append(row)
        return {'table':entry['table'],'head':entry['head'],
                'fields':{f['id']:f['name'] for f in schema['fields']},
                'defaults':dict(defaults),'rows':rows}

    # ---- 写入:写时复制,目录切换为唯一提交点 ----
    def write(self, name, upserts=(), deletes=(), attempts=8):
        for _ in range(attempts):
            entry,head=self.prepare_write(name,upserts,deletes)
            try: return self.commit_write(name,entry,head)
            except ValueError: continue
        raise ValueError('写入冲突重试超限')

    def prepare_write(self, name, upserts=(), deletes=()):
        entry=self._entry(name)
        return entry,self._prepare(entry['head'],upserts,deletes)

    def commit_write(self, name, expected, head):
        new_entry={**expected,'head':head}
        self.catalog.compare_update(name,expected,new_entry)
        return new_entry

    def rename_field(self, name, field_id, new_name):
        if not isinstance(new_name,str) or not new_name: raise ValueError('字段名非法')
        def mutate(schema):
            ids=[f['id'] for f in schema['fields']]
            if field_id not in ids: raise ValueError('字段不存在: %s'%field_id)
            return {'format':1,
                    'fields':[{'id':f['id'],'name':new_name if f['id']==field_id else f['name']} for f in schema['fields']],
                    'defaults':dict(schema['defaults'])}
        return self._commit_schema(name,mutate)

    def add_field(self, name, field_id, field_name, default=_MISSING):
        _check_field_id(field_id)
        if not isinstance(field_name,str) or not field_name: raise ValueError('字段名非法')
        def mutate(schema):
            if field_id in {f['id'] for f in schema['fields']}: raise ValueError('字段已存在: %s'%field_id)
            fields=sorted(schema['fields']+[{'id':field_id,'name':field_name}],key=lambda f:f['id'])
            defaults=dict(schema['defaults'])
            if default is not _MISSING: defaults[field_id]=default
            return {'format':1,'fields':fields,'defaults':defaults}
        return self._commit_schema(name,mutate)

    def _commit_schema(self, name, mutate, attempts=8):
        for _ in range(attempts):
            entry=self._entry(name)
            manifest=self._manifest(entry['head'])
            schema=self._schema(manifest['schema'])
            digest=self.store.put_json(mutate(schema))
            head=self.store.put_json(dict(manifest,schema=digest))
            try:
                self.catalog.compare_update(name,entry,{**entry,'head':head})
                return head
            except ValueError: continue
        raise ValueError('写入冲突重试超限')

    # ---- 写时复制的对象阶段:幂等,可安全重试 ----
    def _prepare(self, head, upserts, deletes):
        upserts=list(upserts);deletes=list(deletes)
        if not upserts and not deletes: return head
        return self._apply(self._manifest(head),upserts,deletes)

    def _apply(self, manifest, upserts, deletes):
        schema=self._schema(manifest['schema'])
        fields={f['id'] for f in schema['fields']}
        seen=set()
        for row in upserts:
            if not isinstance(row,dict) or not isinstance(row.get(ROW_ID),str) or not row[ROW_ID]:
                raise ValueError('行缺少身份')
            rid=row[ROW_ID]
            if rid in seen: raise ValueError('行身份重复: %s'%rid)
            seen.add(rid)
            unknown=set(row)-fields-{ROW_ID}
            if unknown: raise ValueError('字段未在目录中: %s'%sorted(unknown))
        dels=set()
        for rid in deletes:
            if not isinstance(rid,str) or not rid: raise ValueError('删除行身份非法')
            dels.add(rid)
        clash=seen&dels
        if clash: raise ValueError('同一批写入中行身份冲突: %s'%sorted(clash))
        entries=[dict(b) for b in manifest['blocks']]
        firsts=[b['first'] for b in entries]
        def locate(rid):
            if not entries: return None
            i=bisect.bisect_right(firsts,rid)-1
            return i if i>=0 else 0
        plan={}
        for row in upserts:
            i=locate(row[ROW_ID])
            plan.setdefault(-1 if i is None else i,{'u':{},'d':set()})['u'][row[ROW_ID]]=row
        for rid in dels:
            i=locate(rid)
            if i is not None and entries[i]['first']<=rid<=entries[i]['last']:
                plan.setdefault(i,{'u':{},'d':set()})['d'].add(rid)
        new_entries=[]
        boot=plan.pop(-1,None)
        if boot: self._emit(new_entries,sorted(boot['u'].values(),key=lambda r:r[ROW_ID]))
        for i,b in enumerate(entries):
            p=plan.get(i)
            if p is None:
                new_entries.append(b);continue
            block=self.store.json(b['digest'])
            self._validate_block(b,block)
            rows={r[ROW_ID]:r for r in block['rows']}
            for rid in p['d']: rows.pop(rid,None)
            rows.update(p['u'])
            self._emit(new_entries,[rows[k] for k in sorted(rows)])
        new_manifest={'format':2,'schema':manifest['schema'],'blocks':new_entries,
                      'rows':sum(b['rows'] for b in new_entries)}
        return self.store.put_json(new_manifest)

    def _emit(self, out, rows):
        for chunk in self._split(rows):
            digest=self.store.put_json({'format':1,'rows':chunk})
            out.append({'digest':digest,'first':chunk[0][ROW_ID],'last':chunk[-1][ROW_ID],'rows':len(chunk)})

    def _split(self, rows):
        # 按上限顺序切块:块内行只随 row_id 重新分布,身份不变;只重排受影响块,不波及邻块
        return [rows[i:i+self.max_rows] for i in range(0,len(rows),self.max_rows)]

    # ---- 容量账:逻辑量按表计,物理量按全体有效根的并集计 ----
    def _live_pins(self, now):
        return {pid:p for pid,p in self.pins.read().items() if p.get('expires',0)>now}

    def capacity(self, now=None):
        now=time.time() if now is None else now
        entries=self.catalog.read();pins=self._live_pins(now)
        sizes=self.store.list_objects()
        tables={};tables_union=set();union=set()
        for name,e in entries.items():
            objs=self._walk(e['head'])
            tables[name]={'table':e['table'],'head':e['head'],'objects':len(objs),
                          'bytes':sum(sizes.get(d,0) for d in objs)}
            tables_union|=objs
        for p in pins.values(): union|=self._walk(p['head'])
        union|=tables_union
        logical=sum(t['bytes'] for t in tables.values())
        return {'tables':tables,'logical_bytes':logical,
                'shared_bytes':logical-sum(sizes.get(d,0) for d in tables_union),
                'physical_bytes':sum(sizes.get(d,0) for d in union),
                'on_disk_bytes':sum(sizes.values()),
                'pins':[{'pin':pid,'table':p['table'],'head':p['head'],'expires':p['expires']}
                        for pid,p in sorted(pins.items())]}

    def gc_report(self, now=None):
        now=time.time() if now is None else now
        entries=self.catalog.read();pins=self._live_pins(now)
        sizes=self.store.list_objects()
        reachable=set();roots=[]
        for name,e in entries.items():
            reachable|=self._walk(e['head'])
            roots.append({'kind':'table','name':name,'head':e['head']})
        for pid,p in pins.items():
            reachable|=self._walk(p['head'])
            roots.append({'kind':'pin','pin':pid,'table':p['table'],'head':p['head']})
        eligible={d:s for d,s in sizes.items() if d not in reachable}
        return {'roots':roots,
                'reachable_bytes':sum(sizes.get(d,0) for d in reachable),
                'on_disk_bytes':sum(sizes.values()),
                'eligible':{'objects':len(eligible),'bytes':sum(eligible.values()),'digests':sorted(eligible)}}

class ReadHandle:
    """固定在打开时刻的表身份与清单头;保留期内是容量账的有效根。"""
    def __init__(self, tables, name, entry, pin_id, pin_value):
        self._tables=tables;self.name=name;self.entry=entry
        self._pin_id=pin_id;self._pin_value=pin_value;self._closed=False
    def read(self):
        if self._closed: raise ValueError('读句柄已关闭')
        return self._tables._read_entry(self.entry)
    def close(self):
        if not self._closed:
            self._closed=True
            try: self._tables.pins.remove(self._pin_id,self._pin_value)
            except ValueError: pass
    def __enter__(self): return self
    def __exit__(self, *exc): self.close(); return False
