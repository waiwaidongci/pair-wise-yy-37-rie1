from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
class ErrorKind:
    VALIDATION="validation"; NOT_FOUND="not_found"; FORBIDDEN="forbidden"; CONFLICT="conflict"
class DomainError(Exception):
    kind=ErrorKind.VALIDATION
    def __init__(self,message): super().__init__(message); self.message=message
class ValidationError(DomainError): kind=ErrorKind.VALIDATION
class NotFoundError(DomainError): kind=ErrorKind.NOT_FOUND
class PermissionDenied(DomainError): kind=ErrorKind.FORBIDDEN
class ConflictError(DomainError): kind=ErrorKind.CONFLICT
SEVERITIES=['low', 'medium', 'high', 'critical']; STATES=['draft', 'submitted', 'inspection', 'correction', 'approved']; ROLES=['applicant', 'inspector', 'compliance_manager', 'viewer']
@dataclass(frozen=True)
class Item:
    id:int; title:str; description:str; severity:str; quantity:float; threshold:float; status:str; version:int; external_ref:Optional[str]; created_by:str; created_at:str; updated_at:str
@dataclass(frozen=True)
class Record:
    id:int; item_id:int; kind:str; detail:str; status:str; external_ref:Optional[str]; created_by:str; created_at:str
@dataclass(frozen=True)
class AuditEntry:
    id:int; action:str; entity_type:str; entity_id:int; actor:str; detail:Dict[str,Any]; previous_hash:str; entry_hash:str; created_at:str
@dataclass(frozen=True)
class Renewal:
    id:int; request_id:str; facility:str; permit_id:int; permit_version:int; equipment:List[Dict[str,Any]]; emission_limits:Dict[str,float]; status:str; version:int; snapshot:Optional[Dict[str,Any]]; created_by:str; created_at:str; updated_at:str
@dataclass(frozen=True)
class Attachment:
    id:int; renewal_id:int; name:str; content:str; uploaded_by:str; created_at:str
def require_text(value,field,max_length=2000):
    if not isinstance(value,str) or not value.strip(): raise ValidationError(f"{field}不能为空")
    value=value.strip()
    if len(value)>max_length: raise ValidationError(f"{field}不能超过{max_length}个字符")
    return value
def normalize_severity(value):
    if value not in SEVERITIES: raise ValidationError("severity不在允许范围内")
    return value
def require_number(value,field,minimum=0.0):
    if isinstance(value,bool): raise ValidationError(f"{field}必须是数字")
    try: number=float(value)
    except (TypeError,ValueError): raise ValidationError(f"{field}必须是数字")
    if number<minimum: raise ValidationError(f"{field}不能小于{minimum}")
    return number
def ensure_role(role,allowed):
    if role not in allowed: raise PermissionDenied("当前角色无权执行该操作")
def normalize_equipment(value):
    if value is None: return []
    if not isinstance(value,list): raise ValidationError("equipment必须是数组")
    result=[]
    for entry in value:
        if isinstance(entry,str): entry={"name":entry,"pollutants":[]}
        if not isinstance(entry,dict): raise ValidationError("equipment条目必须是对象")
        name=require_text(entry.get("name"),"equipment.name",100)
        pollutants=entry.get("pollutants",[])
        if not isinstance(pollutants,list): raise ValidationError("equipment.pollutants必须是数组")
        result.append({"name":name,"pollutants":[require_text(p,"equipment.pollutants",100) for p in pollutants]})
    return result
def normalize_emission_limits(value):
    if not isinstance(value,dict) or not value: raise ValidationError("emission_limits必须是非空对象")
    result={}
    for key,limit in value.items():
        pollutant=require_text(key,"emission_limits键",100)
        result[pollutant]=require_number(limit,f"emission_limits.{pollutant}",0.000001)
    return result
