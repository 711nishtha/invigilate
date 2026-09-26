import { Navigate, Route, Routes } from 'react-router-dom'

import { RequireTeacher } from './components/RequireTeacher'
import { TeacherLayout } from './components/TeacherLayout'
import { LandingPage } from './pages/LandingPage'
import { NotFoundPage } from './pages/NotFoundPage'
import { AttemptDetailPage } from './pages/teacher/AttemptDetailPage'
import { DashboardPage } from './pages/teacher/DashboardPage'
import { QuizDetailPage } from './pages/teacher/QuizDetailPage'
import { QuizListPage } from './pages/teacher/QuizListPage'
import { QuizNewPage } from './pages/teacher/QuizNewPage'
import { QuizQuestionsPage } from './pages/teacher/QuizQuestionsPage'
import { QuizResultsPage } from './pages/teacher/QuizResultsPage'
import { QuizVariantsPage } from './pages/teacher/QuizVariantsPage'
import { AttemptPage } from './pages/student/AttemptPage'
import { AttemptSubmittedPage } from './pages/student/AttemptSubmittedPage'
import { PrecheckPage } from './pages/student/PrecheckPage'
import { PublicQuizPage } from './pages/student/PublicQuizPage'

/**
 * Route table.
 *
 * Teacher routes sit behind RequireTeacher, which is a rendering convenience
 * only — every one of them is authorized again on the server.
 */
export default function App() {
  return (
    <Routes>
      <Route path="/" element={<LandingPage />} />

      <Route
        path="/teacher"
        element={
          <RequireTeacher>
            <TeacherLayout />
          </RequireTeacher>
        }
      >
        <Route index element={<Navigate to="/teacher/dashboard" replace />} />
        <Route path="dashboard" element={<DashboardPage />} />
        <Route path="quizzes" element={<QuizListPage />} />
        <Route path="quizzes/new" element={<QuizNewPage />} />
        <Route path="quizzes/:id" element={<QuizDetailPage />} />
        <Route path="quizzes/:id/questions" element={<QuizQuestionsPage />} />
        <Route path="quizzes/:id/variants" element={<QuizVariantsPage />} />
        <Route path="quizzes/:id/results" element={<QuizResultsPage />} />
        <Route path="attempts/:attemptId" element={<AttemptDetailPage />} />
      </Route>

      {/* Student routes: no Supabase session required. */}
      <Route path="/q/:publicCode" element={<PublicQuizPage />} />
      {/* A proctored quiz starts here, not at /q/:code: the server refuses
          to create an attempt for one through the direct route. */}
      <Route path="/q/:publicCode/check" element={<PrecheckPage />} />
      <Route path="/attempt/:attemptId" element={<AttemptPage />} />
      <Route path="/attempt/:attemptId/submitted" element={<AttemptSubmittedPage />} />

      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  )
}
