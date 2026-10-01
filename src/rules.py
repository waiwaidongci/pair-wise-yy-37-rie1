from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='空气污染源许可与合规检查'; ENTITY='排污许可'; RENEWAL_ENTITY='续期单'; ID_PREFIX='AQ'
SUPERSEDED='superseded'
SEVERITIES=['low', 'medium', 'high', 'critical']; STATES=['draft', 'submitted', 'inspection', 'correction', 'approved']
ITEM_STATUSES=STATES+[SUPERSEDED]
RENEWAL_STATES=['draft', 'approved']
TRANSITIONS={'draft': ['submitted'], 'submitted': ['inspection'], 'inspection': ['correction'], 'correction': ['approved'], 'approved': [], SUPERSEDED: []}
TRANSITION_ROLES={'submitted': ['applicant'], 'inspection': ['inspector'], 'correction': ['inspector'], 'approved': ['compliance_manager']}
CREATE_ROLES=set(['applicant']); RECORD_ROLES=set(['applicant', 'inspector']); AUDIT_ROLES=set(['compliance_manager', 'viewer']); VIEW_ROLES=set(['applicant', 'inspector', 'compliance_manager', 'viewer'])
RENEWAL_CREATE_ROLES=set(['applicant']); RENEWAL_APPROVE_ROLES=set(['compliance_manager'])
ATTACHMENT_UPLOAD_ROLES=set(['applicant']); ATTACHMENT_VIEW_ALL_ROLES=set(['compliance_manager', 'viewer'])
SEVERITY_WEIGHT={'low': 1.0, 'medium': 3.0, 'high': 6.0, 'critical': 9.0}; DEADLINE_HOURS={'low': 72, 'medium': 24, 'high': 8, 'critical': 4}; TERMINAL_STATES=set(['approved'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
def validate_equipment_list(value,required=False):
    if value is None:
        if required: raise ValidationError("治理设备名单不能为空")
        return []
    if not isinstance(value,list): raise ValidationError("治理设备名单必须是数组")
    if required and len(value)==0: raise ValidationError("治理设备名单不能为空")
    names=[]
    for entry in value:
        if isinstance(entry,dict):
            name=entry.get("name")
        else:
            name=entry
        if not isinstance(name,str) or not name.strip(): raise ValidationError("治理设备名称不能为空")
        name=name.strip()
        if len(name)>100: raise ValidationError("治理设备名称不能超过100个字符")
        if name in names: raise ValidationError(f"治理设备{name}重复")
        names.append(name)
    return names
def can_add_record(item_status):
    return item_status != SUPERSEDED
def renewal_blockers(open_records):
    return ["仍有未关闭整改，不能批准续期"] if open_records>0 else []
def can_view_attachment(role,item,actor,inspector_authorized):
    if role in ATTACHMENT_VIEW_ALL_ROLES: return True
    if role=='applicant' and item.get("created_by")==actor: return True
    if role=='inspector': return bool(inspector_authorized)
    return False
