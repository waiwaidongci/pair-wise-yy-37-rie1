from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='空气污染源许可与合规检查'; ENTITY='排污许可'; ID_PREFIX='AQ'
SEVERITIES=['low', 'medium', 'high', 'critical']; STATES=['draft', 'submitted', 'inspection', 'correction', 'approved']; TRANSITIONS={'draft': ['submitted'], 'submitted': ['inspection'], 'inspection': ['correction'], 'correction': ['approved'], 'approved': []}; TRANSITION_ROLES={'submitted': ['applicant'], 'inspection': ['inspector'], 'correction': ['inspector'], 'approved': ['compliance_manager']}
CREATE_ROLES=set(['applicant']); RECORD_ROLES=set(['applicant', 'inspector']); AUDIT_ROLES=set(['compliance_manager', 'viewer']); VIEW_ROLES=set(['applicant', 'inspector', 'compliance_manager', 'viewer'])
RENEWAL_ENTITY='排污许可续期'; RENEWAL_STATES=['draft', 'submitted', 'approved']; RENEWAL_TRANSITIONS={'draft': ['submitted'], 'submitted': ['approved'], 'approved': []}; RENEWAL_TRANSITION_ROLES={'submitted': ['applicant'], 'approved': ['compliance_manager']}
RENEWAL_CREATE_ROLES=set(['applicant']); ATTACHMENT_UPLOAD_ROLES=set(['applicant']); ATTACHMENT_VIEW_ROLES=set(['applicant', 'compliance_manager'])
INSPECTION_KIND='inspection'; RECTIFICATION_KIND='rectification'; DEFAULT_FACILITY='default'
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
def can_renewal_transition(current,target): return target in RENEWAL_TRANSITIONS.get(current,[])
def validate_renewal_transition(current,target):
    if current not in RENEWAL_STATES or target not in RENEWAL_STATES: raise ValidationError("未知状态")
    if not can_renewal_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def renewal_transition_role(target): return set(RENEWAL_TRANSITION_ROLES.get(target,[]))
def equipment_coverage(equipment):
    covered=set()
    for entry in equipment or []:
        for pollutant in entry.get("pollutants",[]): covered.add(pollutant)
    return covered
def limit_equipment_gaps(emission_limits,equipment):
    covered=equipment_coverage(equipment)
    return sorted(p for p in emission_limits if p not in covered)
def renewal_blockers(open_rectifications):
    blockers=[]
    for record in open_rectifications:
        ref=record.get("external_ref") or "-"
        blockers.append(f"整改未关闭：#{record['id']} {ref} {record['detail']}")
    return blockers
