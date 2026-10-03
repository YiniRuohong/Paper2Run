import { useEffect } from 'react';
import { Link, Navigate, Outlet, Route, Routes, useLocation, useNavigate, useOutletContext } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { getClient, type Project } from './api-client';
import { TopBar } from './components/TopBar';
import { StationBar } from './components/StationBar';
import { SideNav } from './components/SideNav';
import { PageLoading } from './components/Feedback';
import { ProjectsPage } from './features/projects/ProjectsPage';
import { ReviewPage } from './features/papers/ReviewPage';
import { RunPage } from './features/runs/RunPage';
import { ReportPage } from './features/reports/ReportPage';

function navKey(pathname: string): 'projects' | 'review' | 'run' | 'report' {
  if (pathname.startsWith('/papers/')) return 'review';
  if (pathname.includes('/report')) return 'report';
  if (pathname.startsWith('/runs/')) return 'run';
  return 'projects';
}

function AppLayout() {
  const location = useLocation();
  const navigate = useNavigate();

  const sessionQ = useQuery({
    queryKey: ['session'],
    queryFn: () => getClient().getSession(),
    staleTime: 5 * 60_000,
  });
  const projectsQ = useQuery({
    queryKey: ['projects'],
    queryFn: () => getClient().listProjects(),
    enabled: sessionQ.isSuccess,
  });
  const project = projectsQ.data?.items[0];
  const papersQ = useQuery({
    queryKey: ['papers', project?.id],
    queryFn: () => getClient().listPapers(project!.id),
    enabled: !!project,
  });
  const runsQ = useQuery({
    queryKey: ['runs', project?.id],
    queryFn: () => getClient().listRuns(project!.id),
    enabled: !!project,
    refetchInterval: 5000,
  });

  useEffect(() => {
    const onExpired = () => navigate('/login', { state: { from: location.pathname + location.search }, replace: true });
    window.addEventListener('p2r:session-expired', onExpired);
    return () => window.removeEventListener('p2r:session-expired', onExpired);
  }, [navigate, location]);

  if (sessionQ.isPending || (sessionQ.isSuccess && projectsQ.isPending)) {
    return <PageLoading label="连接工作台" />;
  }
  if (!sessionQ.data) {
    return <Navigate to="/login" state={{ from: location.pathname + location.search }} replace />;
  }

  const paper = papersQ.data?.items[0] ?? null;
  const run = runsQ.data?.items[0] ?? null;

  return (
    <>
      <TopBar session={sessionQ.data} />
      <StationBar paper={paper} run={run} />
      <div className="shell">
        <SideNav current={navKey(location.pathname)} paper={paper} run={run} />
        <main className="content">
          <Outlet context={{ project: project ?? null }} />
        </main>
      </div>
    </>
  );
}

export function App() {
  return (
    <Routes>
      <Route element={<AppLayout />}>
        <Route path="/" element={<ProjectsPage />} />
        <Route path="/projects" element={<ProjectsPage />} />
        <Route path="/projects/:projectId" element={<ProjectsPage />} />
        <Route path="/papers/:paperId/review" element={<ReviewPage />} />
        <Route path="/runs/:runId" element={<RunPage />} />
        <Route path="/runs/:runId/report" element={<ReportPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

export function useProject(): Project | null {
  return useOutletContext<{ project: Project | null }>().project;
}

export function HomeLinkFallback() {
  return <Link to="/projects">返回工作台</Link>;
}
