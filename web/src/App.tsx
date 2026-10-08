import { Route, Routes } from 'react-router-dom'
import Layout from './components/Layout'
import OverviewPage from './pages/Overview'
import ServersPage from './pages/Servers'
import JobsPage from './pages/Jobs'
import ResultsPage from './pages/Results'
import ComparePage from './pages/Compare'

export default function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<OverviewPage />} />
        <Route path="/servers" element={<ServersPage />} />
        <Route path="/jobs" element={<JobsPage />} />
        <Route path="/results" element={<ResultsPage />} />
        <Route path="/compare" element={<ComparePage />} />
        <Route path="*" element={<div className="main"><div className="empty">页面不存在</div></div>} />
      </Routes>
    </Layout>
  )
}
