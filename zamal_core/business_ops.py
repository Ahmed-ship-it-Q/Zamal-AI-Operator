"""Central business capabilities composed with existing PersonalOpsWorker and Gate.

No autonomous external writes. Connector authentication remains owned by n8n.
Evidence lives in canonical action results/context, never a second task store.
"""
import datetime,hashlib,json,re,time
from pathlib import Path
from urllib.parse import urlsplit,quote
from .store import Gate,Denied,Conflict,digest,encode,uid
from .integration_security import validate_data,failure
from .safety import Controls
from .worker import Capability,redact
from .public_web import PublicWeb,plain

SAFE=frozenset({'biz_research','biz_jobs','biz_job_prepare','biz_job_track','biz_gmail_brief','biz_calendar_brief','biz_github','biz_plan','biz_status','biz_workflow'})
PROTECTED=frozenset({'biz_email_send','biz_email_forward','biz_email_delete','biz_calendar_write','biz_job_submit','biz_linkedin_write','biz_github_write'})
KINDS=SAFE|PROTECTED
FIELDS={
 'biz_workflow':{'objective','steps'},
 'biz_research':{'query','urls'},'biz_jobs':{'sources','keywords','locations','skills'},
 'biz_job_prepare':{'result_action_id','job_id','candidate'},
 'biz_job_track':{'application_action_id','status','note','follow_up_date'},
 'biz_gmail_brief':{'messages','query'},'biz_calendar_brief':{'events','start','end'},
 'biz_github':{'repository','view'},'biz_plan':{'objective','capabilities'},'biz_status':set(),
 'biz_email_send':{'recipient','subject','body'},'biz_email_forward':{'message_id','recipient'},
 'biz_email_delete':{'message_id'},'biz_calendar_write':{'operation','event'},
 'biz_job_submit':{'application_action_id','target'},'biz_linkedin_write':{'operation','payload'},
 'biz_github_write':{'repository','operation','payload'}}
REQUIRED={k:v for k,v in FIELDS.items() if k in PROTECTED}
REQUIRED.update({'biz_workflow':{'objective','steps'},'biz_research':{'query'},'biz_jobs':{'sources'},'biz_job_prepare':{'result_action_id','job_id','candidate'},'biz_job_track':{'application_action_id','status'},'biz_calendar_brief':{'start','end'},'biz_github':{'view'},'biz_plan':{'objective','capabilities'}})

def validate(kind,p,project):
    if kind not in KINDS or project!='operator' or not isinstance(p,dict):raise Denied('Business operation outside approved Operator scope')
    validate_data(p)
    if set(p)-FIELDS[kind] or REQUIRED.get(kind,set())-set(p):raise Denied('Invalid business action fields')
    if kind=='biz_workflow':
        if not isinstance(p['objective'],str) or not p['objective'].strip() or not isinstance(p['steps'],list) or not 1<=len(p['steps'])<=8:raise Denied('Bounded workflow required')
        for step in p['steps']:
            if not isinstance(step,dict) or set(step)!={'kind','arguments'} or step['kind'] not in KINDS-{'biz_workflow'}:raise Denied('Unknown or recursive workflow step')
            validate(step['kind'],step['arguments'],project)
    if kind=='biz_research':
        if not isinstance(p['query'],str) or not 1<=len(p['query'])<=500:raise Denied('Bounded research query required')
        if 'urls' in p and (not isinstance(p['urls'],list) or not 1<=len(p['urls'])<=5 or not all(isinstance(u,str) for u in p['urls'])):raise Denied('Bounded source list required')
    if kind=='biz_jobs':
        if not isinstance(p['sources'],list) or not 1<=len(p['sources'])<=5:raise Denied('Bounded job sources required')
        for src in p['sources']:
            if not isinstance(src,dict) or set(src)!={'adapter','board'} or src['adapter'] not in ('greenhouse','lever','linkedin') or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',src['board']):raise Denied('Invalid job source')
        for key in ('skills','keywords','locations'):
            if key in p and (not isinstance(p[key],list) or len(p[key])>40 or any(not isinstance(x,str) or not 1<=len(x)<=100 for x in p[key])):raise Denied('Bounded filter required')
    if kind=='biz_github' and ((p['view']!='repositories' and not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',p.get('repository',''))) or p['view'] not in ('status','issues','pulls','checks','repositories')):raise Denied('Only known GitHub read operations allowed')
    if kind=='biz_job_track' and p['status'] not in ('draft','queued','owner_reported_submitted','interview','closed','follow_up_due'):raise Denied('Invalid tracking status')
    if kind=='biz_plan' and (not isinstance(p['capabilities'],list) or not p['capabilities'] or len(p['capabilities'])>8 or set(p['capabilities'])-{'research','jobs','gmail','calendar','github','reasoning','coding','verification'}):raise Denied('Unknown business routing capability')

class BusinessCapability(Capability):
    def run_with_controls(self,*args):raise Denied('Canonical action context required')
    def run_with_action(self,p,baseline,action_id,task_id):
        a=self.worker.store.action(action_id)
        if a['status']!='EXECUTING' or a['task_id']!=task_id or a['kind']!=self.kind or a['payload']!=p:raise Denied('Canonical claim required')
        return self.worker.run(self.kind,p,baseline,task_id,action_id)

class BusinessWorker:
    def __init__(self,store,*,operator=None,cpc=None,web=None):
        from .personal_ops import PersonalOpsWorker
        self.store=store;self.parent=PersonalOpsWorker(store,operator=operator,cpc=cpc);self.paths=self.parent.paths
        self.web=web or PublicWeb()
    def gate(self):return Gate(self.store,{**self.parent.gate()._handlers,**{k:BusinessCapability(self,k) for k in KINDS}})
    def validate(self,kind,p,project):
        if kind in KINDS:validate(kind,p,project)
        else:self.parent.validate(kind,p,project)
    def run(self,kind,p,baseline,task_id,action_id):
        check=lambda:Controls(self.store).check(kind,baseline)
        check();validate(kind,p,self.store.task(task_id)['project'])
        if kind in PROTECTED:
            # Approval grants authorization, never a connector or arbitrary HTTP method.
            return failure('NOT_CONFIGURED','protected_connector_not_connected',protected_side_effects=0)
        if kind=='biz_workflow':r=self.workflow(p,check,task_id,action_id)
        elif kind=='biz_research':r=self.research(p,check)
        elif kind=='biz_jobs':r=self.jobs(p,check)
        elif kind=='biz_job_prepare':r=self.prepare_application(p)
        elif kind=='biz_job_track':r=self.track(p)
        elif kind=='biz_gmail_brief':r=self.gmail(p,check)
        elif kind=='biz_calendar_brief':r=self.calendar(p,check)
        elif kind=='biz_github':r=self.github(p,check)
        elif kind=='biz_plan':r=self.plan(p)
        else:r=self.connection_status()
        check();validate_data(r)
        from .context_bus import ContextBus
        bus=ContextBus(self.store);bus.ensure(task_id)
        with self.store.transaction():bus._event(task_id,None,'BUSINESS_RESULT',{'action_id':action_id,'kind':kind,'result_hash':digest(r),'outcome':r.get('outcome')})
        # Existing context bus and action result are the canonical persistence/audit path.
        return {**r,'task_id':task_id,'action_id':action_id,'result_hash':digest(r),'external_writes':False}
    def workflow(self,p,check,parent_task_id,parent_action_id):
        results=[]
        for i,step in enumerate(p['steps']):
            check()
            task=self.store.create_task(p['objective'],'business_subtask','operator','business:'+parent_action_id+':'+str(i),{'inputs':[{'canonical_parent_task_id':parent_task_id}],'plan':[step],'rules':['Existing Gate is sole authorization authority; no external writes without approval']})
            for state in ('UNDERSTOOD','PLANNED'):task=self.store.transition(task['id'],state,task['revision'])
            action=self.gate().propose(task['id'],step['kind'],step['arguments'],'step')
            if action['approval']:
                result={'ok':False,'outcome':'APPROVAL_REQUIRED','approval_id':action['approval']['id']}
            else:
                try:result=self.gate().execute(action['id'])
                except (Denied,Conflict):result=failure('BLOCKED','child_action_failed_closed')
            results.append({'task_id':task['id'],'action_id':action['id'],'kind':step['kind'],'outcome':result.get('outcome'),'ok':result.get('ok',False),'result_hash':digest(result),'approval_id':result.get('approval_id')})
        return {'ok':all(r['ok'] for r in results),'outcome':'SUCCESS' if all(r['ok'] for r in results) else 'PARTIAL','canonical_parent_task_id':parent_task_id,'children':results,'next_action':'Review canonical child results and any pending Phase 9 approvals'}
    def research(self,p,check):
        urls=p.get('urls')
        if urls is None:
            try:urls=self.web.search(p['query'],check)
            except Denied:return failure('UNAVAILABLE','search_source_unavailable')
        sources=[];errors=[]
        terms=set(re.findall(r'\w{3,}',p['query'].lower()))
        for url in dict.fromkeys(urls):
            check()
            try:
                src=self.web.fetch(url,check);text=plain(src['body']);validate_data({'text':text[:20000]})
                sentences=re.split(r'(?<=[.!?])\s+',text)
                ranked=sorted(enumerate(sentences),key=lambda t:(-len(terms&set(re.findall(r'\w+',t[1].lower()))),t[0]))
                # Short, attributed excerpts; no unsupported generated claims.
                excerpt=' '.join(ranked[0][1].split()[:25]) if ranked else ''
                if not excerpt:raise Denied('Empty source')
                sources.append({k:src[k] for k in ('url','retrieved_at','sha256','content_type')}|{'excerpt':excerpt,'quality':'source_provided_claim_not_independently_verified','domain':urlsplit(url).hostname})
            except Denied:errors.append({'url':url,'code':'source_unavailable_or_denied'})
        independent=len({s['domain'] for s in sources})>=2
        return {'ok':bool(sources),'outcome':'SUCCESS' if sources and not errors else 'PARTIAL' if sources else 'UNAVAILABLE','query_plan':[p['query'],'Compare source claims; verify current primary evidence'],
            'sources':sources,'errors':errors,'independent_domains':independent,'claims_verified':False,
            'comparison':[{'source':s['url'],'claim_excerpt':s['excerpt'],'verification':'requires corroboration'} for s in sources],
            'report':'\n\n'.join(s['excerpt']+' ['+s['domain']+']('+s['url']+')' for s in sources),
            'limitations':['Extractive source comparison; not a verified market conclusion','Publication dates and pricing require source-specific review']}
    def jobs(self,p,check):
        jobs={};errors=[];source_evidence=[]
        for src in p['sources']:
            check();adapter,board=src['adapter'],src['board']
            if adapter=='linkedin':errors.append({'source':'linkedin','code':'authorized_connection_required'});continue
            url=('https://boards-api.greenhouse.io/v1/boards/'+board+'/jobs?content=true' if adapter=='greenhouse' else 'https://api.lever.co/v0/postings/'+board+'?mode=json')
            try:
                raw=self.web.fetch(url,check);data=json.loads(raw['body']);rows=data.get('jobs') if adapter=='greenhouse' else data
                if not isinstance(rows,list):raise ValueError()
                source_evidence.append({k:raw[k] for k in ('url','sha256','retrieved_at')})
                for row in rows[:1000]:
                    title=row.get('title','') if adapter=='greenhouse' else row.get('text','')
                    location=row.get('location',{}).get('name','') if adapter=='greenhouse' else row.get('categories',{}).get('location','')
                    desc=plain(row.get('content','') if adapter=='greenhouse' else row.get('descriptionPlain','')+' '+plain(encode(row.get('lists',[]))))
                    target=row.get('absolute_url','') if adapter=='greenhouse' else row.get('hostedUrl','')
                    text=(title+' '+desc).lower()
                    if p.get('keywords') and not any(x.lower() in text for x in p['keywords']):continue
                    if p.get('locations') and not any(x.lower() in location.lower() for x in p['locations']):continue
                    if not title or not target.startswith('https://'):continue
                    matched=[x for x in p.get('skills',[]) if x.lower() in text];gaps=[x for x in p.get('skills',[]) if x not in matched]
                    ident=digest([board,title.casefold(),location.casefold(),target])
                    jobs[ident]={'id':ident,'source':adapter,'company':board,'title':title,'location':location,'url':target,
                        'requirements_excerpt':desc[:2500],'requirements_hash':digest(desc),'source_hash':raw['sha256'],
                        'match_score':round(100*len(matched)/len(p['skills'])) if p.get('skills') else None,
                        'matched_candidate_skills':matched,'candidate_skills_not_mentioned':gaps,
                        'match_explanation':'Keyword overlap only; eligibility, missing requirements and visa status require review','status':'discovered_not_applied'}
            except (Denied,ValueError,KeyError,TypeError):errors.append({'source':adapter+':'+board,'code':'source_unavailable_or_invalid'})
        ranked=sorted(jobs.values(),key=lambda j:(-(j['match_score'] or 0),j['title'],j['id']))[:20]
        for j in ranked:validate_data(j)
        return {'ok':bool(source_evidence),'outcome':'SUCCESS' if source_evidence and not errors else 'PARTIAL' if source_evidence else 'UNAVAILABLE','jobs':ranked,'sources':source_evidence,'errors':errors,'submission_count':0,'ranking_method':'deterministic_skill_keyword_overlap','coverage':'configured public boards only; not exhaustive'}
    def result_action(self,ident,kind):
        a=self.store.action(ident)
        if a['kind']!=kind or a['status']!='SUCCEEDED' or not a['result']:raise Denied('Verified source action required')
        return a['result']
    def prepare_application(self,p):
        source=self.result_action(p['result_action_id'],'biz_jobs');job=next((j for j in source['jobs'] if j['id']==p['job_id']),None)
        c=p['candidate']
        if not job or not isinstance(c,dict) or set(c)!={'name','skills','experience'} or not isinstance(c['skills'],list) or any(not isinstance(x,str) for x in c['skills']) or not all(isinstance(c[k],str) for k in ('name','experience')):raise Denied('Explicit candidate facts and discovered job required')
        return {'ok':True,'outcome':'SUCCESS','job':job,'source_action_id':p['result_action_id'],'candidate_facts':c,'status':'draft','submitted':False,
            'cv_draft':c['name']+'\nTarget: '+job['title']+'\nSkills (owner supplied): '+', '.join(c['skills'])+'\nExperience (owner supplied): '+c['experience'],
            'cover_letter_draft':'Dear hiring team,\nI wish to be considered for '+job['title']+'. My stated experience is: '+c['experience']+'\nPlease find my resume for review.\n'+c['name'],
            'gaps':['Confirm every job requirement, qualifications, dates and eligibility before submission'],'next_action':'Review draft, then propose biz_job_submit with exact target; never submit automatically'}
    def track(self,p):
        app=self.result_action(p['application_action_id'],'biz_job_prepare')
        if p.get('follow_up_date'):datetime.date.fromisoformat(p['follow_up_date'])
        return {'ok':True,'outcome':'SUCCESS','application_action_id':p['application_action_id'],'job_id':app['job']['id'],'status':p['status'],'note':p.get('note',''),'follow_up_date':p.get('follow_up_date'),'external_submission_verified':False,'recorded_at':time.time()}
    def gmail(self,p,check=lambda:None):
        live=None
        if 'messages' not in p:
            from .connectors import OAuthReadTransport
            live=OAuthReadTransport('gmail',self.paths.roots['operator']).read({'query':p.get('query','in:inbox')},check)
            if live is None:return failure('NOT_CONFIGURED','gmail_oauth_required')
            p={**p,'messages':live['messages']}
        messages=p['messages']
        if not isinstance(messages,list) or len(messages)>50:raise Denied('Bounded message collection required')
        threads={}
        for m in messages:
            if not isinstance(m,dict) or set(m)-{'id','thread_id','subject','sender','snippet','date','outgoing'} or not {'id','thread_id','subject','sender','snippet'}<=set(m) or any(not isinstance(m[k],str) for k in ('id','thread_id','subject','sender','snippet')):raise Denied('Invalid imported message')
            threads.setdefault(m['thread_id'],[]).append(m)
        briefs=[]
        for ident,items in threads.items():
            items=sorted(items,key=lambda x:x.get('date',''));last=items[-1];text=' '.join(x['subject']+' '+x['snippet'] for x in items)
            urgency=any(w in text.lower() for w in ('urgent','deadline','overdue','interview'))
            needs=not last.get('outgoing',False) and ('?' in last['snippet'] or any(w in last['snippet'].lower() for w in ('please','confirm','reply')))
            briefs.append({'thread_id':ident,'message_ids':[m['id'] for m in items],'subject':last['subject'],'summary_excerpt':last['snippet'][:300],'important_heuristic':urgency,'needs_response_heuristic':needs,'follow_up_candidate':bool(last.get('outgoing')),'draft_reply':'Thank you for your message. [Owner: supply the requested information before sending.]' if needs else None})
        return {'ok':True,'outcome':'SUCCESS','origin':'live_n8n_gmail' if live else 'explicitly_imported_data_not_live_gmail','threads':briefs,'pending_communication':[b['thread_id'] for b in briefs if b['needs_response_heuristic'] or b['follow_up_candidate']],'sent':False,'classification':'heuristic_requires_review'}
    def calendar(self,p,check=lambda:None):
        live=None
        if 'events' not in p:
            from .connectors import OAuthReadTransport
            live=OAuthReadTransport('calendar',self.paths.roots['operator']).read({'start':p['start'],'end':p['end']},check)
            if live is None:return failure('NOT_CONFIGURED','calendar_oauth_required')
            p={**p,'events':live['events']}
        def dt(v):
            d=datetime.datetime.fromisoformat(v.replace('Z','+00:00'))
            if d.tzinfo is None:raise Denied('Timezone-qualified interval required')
            return d
        start,end=dt(p['start']),dt(p['end'])
        if end<=start:raise Denied('Invalid calendar range')
        events=p['events']
        if not isinstance(events,list) or len(events)>100:raise Denied('Bounded events required')
        intervals=[]
        for e in events:
            if not isinstance(e,dict) or set(e)!={'id','title','start','end'}:raise Denied('Invalid event')
            a,b=dt(e['start']),dt(e['end'])
            if a>=b:raise Denied('Invalid event interval')
            if a<end and b>start:intervals.append((max(a,start),min(b,end),e))
        intervals.sort(key=lambda x:(x[0],x[2]['id']));conflicts=[];free=[];cursor=start
        for i,(a,b,e) in enumerate(intervals):
            for aa,bb,ee in intervals[:i]:
                if a<bb and aa<b:conflicts.append([ee['id'],e['id']])
            if a>cursor:free.append({'start':cursor.isoformat(),'end':a.isoformat()})
            cursor=max(cursor,b)
        if cursor<end:free.append({'start':cursor.isoformat(),'end':end.isoformat()})
        return {'ok':True,'outcome':'SUCCESS','origin':'live_n8n_calendar' if live else 'explicitly_imported_data_not_live_calendar','agenda':[e for _,_,e in intervals],'conflicts':conflicts,'free_intervals':free,'meeting_preparation':[{'event_id':e['id'],'next_action':'Review objective, attendees and relevant notes; prepare follow-up draft'} for _,_,e in intervals],'external_writes':False}
    def github(self,p,check):
        from .connectors import OAuthReadTransport
        connected=OAuthReadTransport('github',self.paths.roots['operator']).read(p,check)
        if connected is not None:return {'ok':True,'outcome':'SUCCESS','access':'authenticated_n8n_github',**connected}
        if p['view']=='repositories':return failure('NOT_CONFIGURED','github_oauth_required')
        repo=p['repository'];suffix={'status':'','issues':'/issues?state=open&per_page=10','pulls':'/pulls?state=open&per_page=10','checks':'/actions/runs?per_page=10'}[p['view']]
        src=self.web.fetch('https://api.github.com/repos/'+repo+suffix,check);data=json.loads(src['body'])
        if p['view']=='status':result={k:data.get(k) for k in ('full_name','description','default_branch','open_issues_count','updated_at','html_url')}
        else:
            rows=data.get('workflow_runs',[]) if p['view']=='checks' else data
            if not isinstance(rows,list):raise Denied('Invalid GitHub response')
            result=[{k:r.get(k) for k in ('id','number','title','name','state','status','conclusion','html_url','updated_at')} for r in rows[:10]]
        return {'ok':True,'outcome':'SUCCESS','repository':repo,'view':p['view'],'data':result,'source':{k:src[k] for k in ('url','sha256','retrieved_at')},'access':'public_unauthenticated','next_action':'Review CI failures or issues and prepare scoped development handoff; no repository writes'}
    def plan(self,p):
        route={'research':'biz_research','jobs':'biz_jobs','gmail':'biz_gmail_brief','calendar':'biz_calendar_brief','github':'biz_github','reasoning':'ai_inference:planner','coding':'ai_inference:coder','verification':'ai_inference:verifier'}
        return {'ok':True,'outcome':'SUCCESS','objective':p['objective'],'steps':[{'capability':k,'handler':route[k],'state':'NEEDS_SCOPED_ARGUMENTS','approval':'existing_Phase9_policy','owner':'operator'} for k in dict.fromkeys(p['capabilities'])],'controller':'Zamal Operator','next_action':'Bind exact scoped arguments to canonical child tasks; providers only return drafts'}
    def connection_status(self):
        return {'ok':True,'outcome':'SUCCESS','gmail':'IMPLEMENTED_AWAITING_AUTH','calendar':'IMPLEMENTED_AWAITING_AUTH','github_private':'IMPLEMENTED_AWAITING_AUTH','github_public':'AVAILABLE_NOT_ACTIVE','linkedin':'IMPLEMENTED_AWAITING_AUTH','research':'AVAILABLE_NOT_ACTIVE','jobs':'AVAILABLE_NOT_ACTIVE','openai':'AVAILABLE_NOT_ACTIVE'}
