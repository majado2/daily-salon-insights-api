from fastapi import APIRouter

from app.api import admin_days, admin_master, auth, cashier, reports

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(cashier.router)
api_router.include_router(admin_days.router)
api_router.include_router(admin_master.router)
api_router.include_router(reports.router)
