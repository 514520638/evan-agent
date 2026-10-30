"""小规模个人知识库 RAG：带偏移的切分、SQLite 向量缓存、BM25/RRF/MMR。

权限候选来自 KnowledgeStore；未经授权的资料不参加嵌入或排序。
Embedding 使用单独配置的兼容接口，未配置／失败时明确回退 BM25。
"""

from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import math
import os
import re
from collections import Counter

import httpx
from evan_agent.conversation_db import private_connection


@dataclass(frozen=True)
class Chunk:
    id: str
    entry_id: str
    text: str
    start: int
    end: int


def split_entry(entry, size=600, overlap=100):
    """短事实不拆；长文优先按段落、换行、中文句末切分，保留真实字符偏移。"""
    if not 0 <= overlap < size or size < 50:
        raise ValueError('chunk 参数须满足 size>=50 且 0<=overlap<size。')
    start = 0
    chunks = []
    while start < len(entry.content):
        end = min(start + size, len(entry.content))
        if end < len(entry.content):
            for pattern in (r'\n\n', r'\n', r'[。！？；.!?;]'):
                boundaries = list(re.finditer(pattern, entry.content[start + size // 2:end]))
                if boundaries:
                    end = start + size // 2 + boundaries[-1].end()
                    break
        text = entry.content[start:end]
        if text.strip():
            digest = hashlib.sha256(f'{entry.id}:{start}:{end}:{text}'.encode()).hexdigest()[:24]
            chunks.append(Chunk(digest, entry.id, text, start, end))
        if end == len(entry.content):
            break
        start = max(start + 1, end - overlap)
    return chunks


def tokens(text):
    result = []
    for word in re.findall(r'[a-z0-9_+#.]+|[\u4e00-\u9fff]+', text.lower()):
        if re.fullmatch(r'[\u4e00-\u9fff]+', word):
            result.extend(word[i:i + 2] for i in range(len(word) - 1))
            if len(word) == 1:
                result.append(word)
        else:
            result.append(word)
    return result


def cosine(a, b):
    if len(a) != len(b):
        raise ValueError('向量维度不一致。')
    norm = math.sqrt(sum(x*x for x in a) * sum(x*x for x in b))
    return sum(x*y for x,y in zip(a,b)) / norm if norm else 0.0


class EmbeddingClient:
    """只使用显式 Embedding 配置，绝不复用 MiniMax 聊天凭据。"""
    def __init__(self):
        self.base = os.getenv('EMBEDDING_BASE_URL', '').rstrip('/')
        self.model = os.getenv('EMBEDDING_MODEL', '')
        self.key = os.getenv('EMBEDDING_API_KEY', '')
        self.enabled = bool(self.base and self.model)
        self.fingerprint = hashlib.sha256(f'{self.base}:{self.model}:paragraph-v1'.encode()).hexdigest()

    def encode(self, texts):
        if not self.enabled:
            raise ValueError('Embedding 未配置。')
        headers = {'Authorization': f'Bearer {self.key}'} if self.key else {}
        result = []
        for start in range(0,len(texts),32):
            batch = texts[start:start+32]
            with httpx.Client(timeout=20) as client:
                response = client.post(self.base+'/embeddings', headers=headers,
                                       json={'model':self.model,'input':batch})
                response.raise_for_status()
                data = sorted(response.json()['data'], key=lambda item:item['index'])
            if [item['index'] for item in data] != list(range(len(batch))):
                raise ValueError('Embedding 响应数量或顺序错误。')
            for item in data:
                vector = item['embedding']
                if not vector or any(type(v) not in (int,float) or not math.isfinite(v) for v in vector) or not any(vector):
                    raise ValueError('Embedding 返回无效向量。')
                result.append(vector)
        if len({len(v) for v in result}) > 1:
            raise ValueError('Embedding 返回不一致的维度。')
        return result


class Retriever:
    def __init__(self, store, embedder=None):
        self.store = store
        self.embedder = embedder if embedder is not None else EmbeddingClient()
        self.path = store.path.with_name(store.path.stem+'.vectors.sqlite3')
        with closing(private_connection(self.path)) as db,db:
            db.execute('''CREATE TABLE IF NOT EXISTS vectors (
                chunk_id TEXT NOT NULL, fingerprint TEXT NOT NULL, entry_id TEXT NOT NULL,
                vector TEXT NOT NULL, PRIMARY KEY(chunk_id,fingerprint))''')

    def invalidate(self, entry_id):
        with closing(private_connection(self.path)) as db,db:
            db.execute('DELETE FROM vectors WHERE entry_id=?',(entry_id,))

    def search(self, identity, query, limit=5, offset=0):
        if not isinstance(query,str) or len(query)>200:
            raise ValueError('查询最多 200 字符。')
        if type(limit) is not int or not 1 <= limit <= 10 or type(offset) is not int or offset<0:
            raise ValueError('分页参数无效。')
        # 所有原文先通过 SQL 可见性过滤；不存在先召回私密向量再交给模型过滤的路径。
        entries = []
        page = 0
        while True:
            batch = self.store.list_entries(identity, limit=200, offset=page)
            entries.extend(entry for entry in batch if entry.status=='published')
            if len(batch)<200: break
            page += 200
        if not query.strip():
            return [(entry, None) for entry in entries[offset:offset+limit+1]], {'mode':'browse','reranker':'none'}
        by_id = {entry.id:entry for entry in entries}
        chunks = [chunk for entry in entries for chunk in split_entry(entry)]
        if not chunks:
            return [], {'mode':'bm25','reranker':'mmr'}
        counts = [Counter(tokens(by_id[c.entry_id].category+' '+c.text)) for c in chunks]
        terms = set(tokens(query))
        average = sum(sum(c.values()) for c in counts)/len(counts) or 1
        frequencies = Counter(term for count in counts for term in count)
        lexical = []
        for i, count in enumerate(counts):
            score = 0
            for term in terms:
                frequency = frequencies[term]
                tf = count[term]
                if tf:
                    idf = math.log(1+(len(chunks)-frequency+.5)/(frequency+.5))
                    score += idf*tf*2.2/(tf+1.2*(.25+.75*sum(count.values())/average))
            if score>0: lexical.append((i,score))
        lexical.sort(key=lambda x:-x[1])
        vectors = None
        semantic = []
        reason = 'embedding_not_configured'
        if self.embedder.enabled:
            try:
                with closing(private_connection(self.path)) as db:
                    cached = {}
                    ids = list(by_id)
                    for start in range(0,len(ids),100):
                        batch = ids[start:start+100]
                        slots = ','.join('?' for _ in batch)
                        cached.update(db.execute(f'SELECT chunk_id,vector FROM vectors WHERE fingerprint=? AND entry_id IN ({slots})',
                                                 [self.embedder.fingerprint,*batch]).fetchall())
                missing = [chunk for chunk in chunks if chunk.id not in cached]
                if missing:
                    encoded = self.embedder.encode([by_id[c.entry_id].category+'\n'+c.text for c in missing])
                    if len(encoded)!=len(missing): raise ValueError('Embedding 数量不一致。')
                    with closing(private_connection(self.path)) as db,db:
                        for chunk,vector in zip(missing,encoded):
                            cached[chunk.id] = json.dumps(vector)
                            db.execute('INSERT OR REPLACE INTO vectors VALUES (?,?,?,?)',
                                       (chunk.id,self.embedder.fingerprint,chunk.entry_id,cached[chunk.id]))
                vectors = [json.loads(cached[c.id]) for c in chunks]
                qvector = self.embedder.encode([query])[0]
                threshold = float(os.getenv('RAG_MIN_SIMILARITY','0.3'))
                semantic = [(i,cosine(qvector,v)) for i,v in enumerate(vectors)]
                semantic = sorted((x for x in semantic if x[1]>=threshold),key=lambda x:-x[1])
                reason = None
            except (httpx.HTTPError,ValueError,KeyError,TypeError,IndexError):
                # 不输出供应商异常正文，其中可能包含 Key 或私密文档。
                reason = 'embedding_unavailable'
                semantic = [];vectors=None
        # Reciprocal Rank Fusion 合并独立召回列表，不把 BM25 分值和余弦直接相加。
        fused = {}
        for ranking in (lexical[:40],semantic[:40]):
            for rank,(i,_) in enumerate(ranking,1): fused[i]=fused.get(i,0)+1/(60+rank)
        remaining = set(fused)
        selected = []
        # MMR 在相关性基础上压制同一资料的重叠块；这是算法重排，不是 CrossEncoder。
        while remaining:
            def score(i):
                redundancy = max((max(0,cosine(vectors[i],vectors[j])) if vectors else
                                  len(set(counts[i]) & set(counts[j]))/max(1,len(set(counts[i]) | set(counts[j])))
                                  for j in selected),default=0)
                return .8*fused[i]/max(fused.values())-.2*redundancy
            best = max(remaining,key=lambda i:(score(i),-i))
            selected.append(best);remaining.remove(best)
        # 每条原始资料只返回最佳块，避免一个长项目占满上下文。
        unique=[];seen=set()
        for i in selected:
            if chunks[i].entry_id not in seen:
                seen.add(chunks[i].entry_id);unique.append(i)
        results=[]
        for i in unique[offset:offset+limit+1]:
            chunk=chunks[i];entry=by_id[chunk.entry_id]
            # 返回前再次查当前权限／状态／版本，避免索引期间资料被撤销或修改。
            try: latest=self.store.get_published(identity,entry.id)
            except LookupError: continue
            if latest.updated_at==entry.updated_at: results.append((latest,chunk))
        return results, {'mode':'hybrid' if reason is None else 'bm25','fallback_reason':reason,
                         'reranker':'mmr','embedding_model':self.embedder.model if reason is None else None}


def main():
    """本地诊断入口，不需要通过模型决定是否调用搜索工具。"""
    import argparse
    from dotenv import load_dotenv
    from evan_agent.knowledge import KnowledgeStore
    from evan_agent.auth import AuthContext, AuthStore, read_password
    load_dotenv('.env')
    parser=argparse.ArgumentParser(description='知识检索诊断')
    sub=parser.add_subparsers(dest='action',required=True)
    sub.add_parser('status')
    search=sub.add_parser('search')
    search.add_argument('query')
    search.add_argument('--owner',action='store_true')
    args=parser.parse_args()
    if args.action=='status':
        embedder=EmbeddingClient()
        print('Embedding:', 'configured' if embedder.enabled else 'not configured; BM25 fallback')
        print('Embedding model:', embedder.model or '(none)')
        print('Chunk: 600 characters / 100 overlap; fusion: RRF; reranker: MMR')
        return
    identity=AuthStore().login(read_password('本人密码：')) if args.owner else AuthContext.guest()
    results,meta=Retriever(KnowledgeStore()).search(identity,args.query)
    print(json.dumps(meta,ensure_ascii=False))
    for entry,chunk in results[:5]:
        print(f'[资料:{entry.id}] chunk={chunk.id if chunk else "whole"}\n{chunk.text if chunk else entry.content[:1600]}')


if __name__=='__main__':
    main()
