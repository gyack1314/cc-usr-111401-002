"""多父提交节点与分支引用的基础读写。"""
from .catalog import Catalog

class Branches:
    def __init__(self,store,root):self.store=store;self.refs=Catalog(root)
    def commit(self,parents,rows,schema):
        if len(parents)>2:raise ValueError('父提交数量超限')
        for p in parents:self.store.json(p)
        if len({r['row_id'] for r in rows})!=len(rows):raise ValueError('行身份重复')
        return self.store.put_json({'parents':parents,'rows':self.store.put_json(rows),'schema':schema})
    def closure(self,head):
        seen=set();pending=[head]
        while pending:
            node=pending.pop()
            if node in seen:continue
            seen.add(node);pending.extend(self.store.json(node)['parents'])
        return seen
    def read(self,head):return self.store.json(self.store.json(head)['rows'])

def inspect(data,store,root):
    b=Branches(store,root/'branches');ids={}
    for c in data['commits']:
        ids[c['id']]=b.commit([ids[p] for p in c['parents']],c['rows'],{'10':'工具','20':'团队','30':'版本'})
    for name,head in data['refs'].items():b.refs.compare_update(name,None,ids[head])
    return {'refs':b.refs.read(),'ancestry_size':{n:len(b.closure(h)) for n,h in b.refs.read().items()}}
