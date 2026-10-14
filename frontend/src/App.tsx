import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import AppShell from './components/Layout/AppShell'
import RequireAuth from './components/RequireAuth'
import Login from './pages/Login'
import Dashboard from './pages/Dashboard'
import StructureDetail from './pages/StructureDetail'
import InspectionDetail from './pages/InspectionDetail'
import ComparePage from './pages/ComparePage'
import DocsPage from './pages/DocsPage'
import UsersPage from './pages/Users'
import AccountPage from './pages/Account'

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route
          element={
            <RequireAuth>
              <AppShell />
            </RequireAuth>
          }
        >
          <Route path="/" element={<Dashboard />} />
          <Route path="/docs" element={<DocsPage />} />
          <Route path="/account" element={<AccountPage />} />
          <Route
            path="/users"
            element={
              <RequireAuth adminOnly>
                <UsersPage />
              </RequireAuth>
            }
          />
          <Route path="/structures/:structureId" element={<StructureDetail />} />
          <Route
            path="/structures/:structureId/inspections/:inspectionId"
            element={<InspectionDetail />}
          />
          <Route
            path="/structures/:structureId/compare"
            element={<ComparePage />}
          />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  )
}
