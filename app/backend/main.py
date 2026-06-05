from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routes.sessions import router

app = FastAPI(title="INT Meeting Recorder API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
