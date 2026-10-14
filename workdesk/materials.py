"""Frozen, scoped material retrieval shared by model and Agent entrypoints."""
import base64
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET
if __package__:
    from .store import DeskError
else:
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from workdesk.store import DeskError

TEXT={'.md','.txt','.csv','.tsv','.json'}
IMAGES={'.png','.jpg','.jpeg','.webp','.gif'}
OFFICE={'.docx','.xlsx','.pptx'}

def extract(path):
    path=Path(path);ext=path.suffix.lower()
    if ext in TEXT:
        data=path.read_bytes()
        encodings=('utf-16',) if data.startswith((b'\xff\xfe',b'\xfe\xff')) else ('utf-8-sig','gb18030')
        for encoding in encodings:
            try:return data.decode(encoding),'文本'
            except UnicodeError:continue
        raise DeskError('无法识别文本编码')
    if ext in OFFICE:
        with zipfile.ZipFile(path) as z:
            if sum(i.file_size for i in z.infolist())>64*1024**2:raise DeskError('文档解压后过大，未读取')
            if ext=='.docx':names=['word/document.xml']
            elif ext=='.pptx':names=sorted(n for n in z.namelist() if re.fullmatch(r'ppt/slides/slide\d+\.xml',n))
            else:names=sorted(n for n in z.namelist() if re.fullmatch(r'xl/worksheets/sheet\d+\.xml',n))
            shared=[]
            if ext=='.xlsx' and 'xl/sharedStrings.xml' in z.namelist():
                root=ET.fromstring(z.read('xl/sharedStrings.xml'))
                shared=[''.join(e.itertext()) for e in root]
            parts=[]
            for n in names:
                root=ET.fromstring(z.read(n));parts.append('['+n+']')
                if ext=='.xlsx':
                    for row in root.iter('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row'):
                        values=[]
                        for cell in row:
                            v=cell.find('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v')
                            text=v.text if v is not None and v.text else ''.join(cell.itertext())
                            if cell.get('t')=='s' and text.isdigit() and int(text)<len(shared):text=shared[int(text)]
                            values.append(cell.get('r','')+': '+text)
                        parts.append(' | '.join(values))
                else:
                    for node in root.iter():
                        if node.tag.rsplit('}',1)[-1]=='t' and node.text:parts.append(node.text)
            return '\n'.join(parts),'仅提取正文/单元格文字；图表、文档图片、批注和公式计算未核验'
    if ext=='.pdf':
        try:
            from pypdf import PdfReader
            doc=PdfReader(path);pages=['[第%d页]\n%s'%(i+1,p.extract_text() or '[本页未提取到文字]') for i,p in enumerate(doc.pages)]
            return '\n\n'.join(pages),'PDF文字层；扫描图片、版式和图表未核验'
        except ImportError:
            python=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
            if not python.is_file():raise DeskError('PDF文字读取组件不可用，可由 Agent 读取')
            proc=subprocess.run([str(python),str(Path(__file__).resolve()),'--pdf',str(path)],capture_output=True,
                timeout=45,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if proc.returncode:raise DeskError('PDF文字提取失败，可能为加密或扫描文档')
            return proc.stdout.decode('utf8'),'PDF文字层；扫描图片、版式和图表未核验'
    raise DeskError('此文件类型暂不支持自动读取，可由 Agent 读取')

def background_files(roots):
    out=[]
    for raw in roots:
        p=Path(raw)
        if not p.exists() or p.is_symlink() or p.is_junction():raise DeskError('配置的背景资料不存在或为目录链接')
        if p.is_file():candidates=[p]
        else:
            candidates=[]
            for folder,dirs,files in os.walk(p,followlinks=False):
                dirs[:]=[d for d in dirs if not d.startswith('.') and not (Path(folder)/d).is_junction() and not (Path(folder)/d).is_symlink()]
                candidates.extend(Path(folder)/n for n in files if not n.startswith('.'))
                if len(candidates)>500:raise DeskError('背景目录文件过多，请选择具体文件或较小目录')
        for item in sorted(candidates):
            if item.suffix.lower() not in TEXT|OFFICE|{'.pdf'} or item.is_symlink():continue
            if any(s in item.name.lower() for s in ('credential','password','secret','settings.json')):continue
            if item.stat().st_size>24*1024**2:raise DeskError('背景文件超过24MB，请分拆或交给 Agent')
            out.append((hashlib.sha256(str(item.resolve()).encode('utf8')).hexdigest()[:12]+'-'+item.name,item))
    return out

class Pool:
    def __init__(self,context,ledger=None):
        self.context=context;self.sources={s['id']:s for s in context['sources']}
        self.cache={};self.ledger=ledger if ledger is not None else {}
    def source(self,sid):
        if sid not in self.sources:raise DeskError('资料不属于此轮授权范围')
        s=self.sources[sid];p=Path(s['snapshot_path'])
        if s.get('cleaned_at'):raise DeskError('此资料已清理；请检索历史记忆或从外部备份恢复')
        if not p.is_file() or p.is_symlink() or hashlib.sha256(p.read_bytes()).hexdigest()!=s['sha256']:
            raise DeskError('资料快照丢失或哈希变化，未读取')
        return s,p
    def text(self,sid):
        if sid not in self.cache:
            _,p=self.source(sid)
            self.cache[sid]=extract(p)
        return self.cache[sid]
    def read(self,sid,start=1,end=None,budget=14000):
        text,method=self.text(sid);lines=text.splitlines() or ['']
        if type(start)is not int or start<1 or start>len(lines):raise DeskError('读取起始行无效')
        end=min(len(lines),end or start+249)
        if type(end)is not int or end<start:raise DeskError('读取结束行无效')
        selected=[];size=0
        for line in lines[start-1:end]:
            if size+len(line)>budget and selected:break
            if len(line)>budget:raise DeskError('单行材料过长，请由 Agent 分段读取')
            selected.append(line);size+=len(line)+1
        actual=start+len(selected)-1
        return dict(source_id=sid,object_id=self.sources[sid]['object_id'],relative_path=self.sources[sid]['relative_path'],
                    start_line=start,end_line=actual,total_lines=len(lines),method=method,text='\n'.join(selected))
    def chunks(self,sid,budget):
        text,_=self.text(sid);lines=text.splitlines() or [''];start=1
        while start<=len(lines):
            part=self.read(sid,start,len(lines),budget);yield part;start=part['end_line']+1
    def search(self,query,limit=5):
        if not isinstance(query,str) or not query.strip() or len(query)>200:raise DeskError('检索词须为1到200字')
        terms=[x for x in re.split(r'[\s,，;；]+',query.strip()) if x];matches=[]
        documents=0
        for sid,s in self.sources.items():
            ext=Path(s['relative_path']).suffix.lower()
            path_hit=any(t.lower() in s['relative_path'].lower() for t in terms)
            if ext not in TEXT:
                if ext not in OFFICE|{'.pdf'} or not path_hit or documents>=2:continue
                documents+=1
            try:text,_=self.text(sid)
            except (DeskError,OSError):continue
            lines=text.splitlines();positions=[]
            for i,line in enumerate(lines):
                score=sum(line.lower().count(t.lower()) for t in terms)
                if score:positions.append((score,i))
            if not positions and path_hit:positions=[(1,0)]
            for score,i in sorted(positions,reverse=True)[:2]:
                # Include neighboring messages/paragraphs, not only a keyword hit.
                lo=max(1,i-12);hi=min(len(lines),i+22)
                try:part=self.read(sid,lo,hi,4000)
                except DeskError:continue
                matches.append((score,part))
        matches.sort(key=lambda x:x[0],reverse=True)
        return [m[1] for m in matches[:max(1,min(limit,8))]]
    def image(self,sid):
        _,path=self.source(sid)
        if path.suffix.lower() not in IMAGES or path.stat().st_size>12*1024**2:raise DeskError('图片不支持或超过12MB')
        if path.suffix.lower()=='.gif':raise DeskError('动画表情暂交给 Agent 核验')
        mime=mimetypes.guess_type(path.name)[0] or 'image/jpeg'
        return 'data:'+mime+';base64,'+base64.b64encode(path.read_bytes()).decode('ascii')
    def mark(self,parts):
        for part in parts:
            sid=part['source_id'];item=self.ledger.setdefault(sid,dict(ranges=[],total_lines=part['total_lines'],method=part['method']))
            item['ranges'].append([part['start_line'],part['end_line']])
    def coverage(self):
        out=[]
        for sid,s in self.sources.items():
            info=self.ledger.get(sid)
            if not info:out.append(dict(source_id=sid,state='unread',note='本轮未向模型提供此文件；未作为结论依据'));continue
            ranges=sorted(info.get('ranges',[]));finish=0
            for lo,hi in ranges:
                if lo>finish+1:break
                finish=max(finish,hi)
            full=finish>=info['total_lines'] and info['method']=='文本'
            out.append(dict(source_id=sid,state='read' if full else 'partial',
                note=(f"已提供行段 {ranges}；{info['method']}" if ranges else info['method'])[:1900]))
        return out

if __name__=='__main__' and len(sys.argv)==3 and sys.argv[1]=='--pdf':
    sys.stdout.reconfigure(encoding='utf8')
    text,_=extract(Path(sys.argv[2]));print(text)
