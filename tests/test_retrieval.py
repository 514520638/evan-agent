"""验证索引权限、增量更新、混合检索链路；替身向量不用于宣称真实模型效果。"""
from pathlib import Path
from tempfile import TemporaryDirectory
from dataclasses import replace
from contextlib import closing
import sqlite3,time,unittest
from evan_agent.auth import AuthContext
from evan_agent.knowledge import KnowledgeStore
from evan_agent.retrieval import Retriever,split_entry

class SemanticFixture:
    enabled=True;model='fixture';fingerprint='fixture-v1'
    def __init__(self): self.inputs=[]
    def encode(self,texts):
        self.inputs.extend(texts)
        return [[1.,0.] if any(word in text for word in ('任职','工作','职业')) else [0.,1.] for text in texts]

class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=KnowledgeStore(Path(self.temp.name)/'knowledge.sqlite3')
        self.owner=AuthContext('a','owner','owner',time.monotonic()+60)
        self.guest=AuthContext.guest()
        self.emb=SemanticFixture();self.rag=Retriever(self.store,self.emb)
    def add(self,text,**kw):
        return self.store.create(self.owner,content=text,visibility=kw.pop('visibility','public'),status=kw.pop('status','published'),**kw)
    def test_semantic_recall_without_literal_overlap(self):
        entry=self.add('我任职于示例科技。')
        self.assertEqual(self.store.search(self.guest,'工作'),[])
        hits,meta=self.rag.search(self.guest,'工作')
        self.assertEqual(hits[0][0].id,entry.id);self.assertEqual(meta['mode'],'hybrid')
    def test_permissions_and_cache_and_updates(self):
        entry=self.add('任职示例')
        self.add('私密工作',visibility='private');self.add('草稿工作',status='draft')
        self.rag.search(self.guest,'工作')
        self.assertFalse(any('私密' in t or '草稿' in t for t in self.emb.inputs))
        count=len(self.emb.inputs);self.rag.search(self.guest,'工作')
        self.assertEqual(len(self.emb.inputs),count+1)
        self.store.update(self.owner,entry.id,visibility='private')
        self.assertEqual(self.rag.search(self.guest,'工作')[0],[])
        with closing(sqlite3.connect(self.rag.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM vectors WHERE entry_id=?',(entry.id,)).fetchone()[0],0)
    def test_chunk_offsets_and_size(self):
        entry=self.add(('工作章节。\n\n项目描述。'*150))
        chunks=split_entry(entry)
        self.assertGreater(len(chunks),1)
        for c in chunks:
            self.assertEqual(c.text,entry.content[c.start:c.end]);self.assertLessEqual(len(c.text),600)
        self.assertEqual(chunks[-1].end,len(entry.content))
        self.assertEqual(len({c.id for c in chunks}),len(chunks))
    def test_failure_falls_back_explicitly_and_expiry_denied(self):
        self.add('我在北京工作')
        self.emb.encode=lambda text: (_ for _ in ()).throw(ValueError('bad data'))
        hits,meta=self.rag.search(self.guest,'北京')
        self.assertTrue(hits);self.assertEqual(meta['fallback_reason'],'embedding_unavailable')
        with self.assertRaises(PermissionError): self.rag.search(replace(self.owner,expires_at=0),'北京')
    def test_version_fingerprint_separation_and_delete(self):
        entry=self.add('任职于示例')
        self.rag.search(self.guest,'工作')
        self.emb.fingerprint='new-model';self.emb.inputs=[]
        self.rag.search(self.guest,'工作');self.assertEqual(len(self.emb.inputs),2)
        self.store.delete(self.owner,entry.id)
        self.assertEqual(self.rag.search(self.guest,'工作')[0],[])

if __name__=='__main__': unittest.main()
