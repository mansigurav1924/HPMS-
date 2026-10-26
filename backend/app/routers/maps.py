from fastapi import APIRouter, Depends, Request, HTTPException
from pydantic import BaseModel, Field
from typing import Dict, Any
from ..db.session import require_role
from ..services.maps_service import geocode, reverse_geocode, GeocodeResponse
from ..core.rate_limit import limiter

router = APIRouter(prefix="/maps", tags=["maps"])

class GeocodeRequest(BaseModel):
    address: str = Field(..., min_length=3, max_length=255)

class ReverseGeocodeRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lng: float = Field(..., ge=-180, le=180)

def require_owner_or_admin(request: Request) -> Dict[str, Any]:
    claims = require_role(request, "owner", "admin")
    return claims

@router.post("/geocode", response_model=GeocodeResponse)
@limiter.limit("30/minute")
async def api_geocode(request: Request, body: GeocodeRequest, claims: dict = Depends(require_owner_or_admin)):
    return await geocode(body.address)

@router.post("/reverse-geocode", response_model=GeocodeResponse)
@limiter.limit("30/minute")
async def api_reverse_geocode(request: Request, body: ReverseGeocodeRequest, claims: dict = Depends(require_owner_or_admin)):
    if body.lat == 0 and body.lng == 0:
        raise HTTPException(status_code=400, detail="Invalid coordinates (0,0)")
    return await reverse_geocode(body.lat, body.lng)
