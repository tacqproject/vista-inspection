@echo off
cd frontend
if not exist node_modules (
    npm install
)
npm run dev
