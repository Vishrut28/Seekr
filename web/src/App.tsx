import { Navigate, Route, HashRouter as Router, Routes } from "react-router-dom";
import { Loading } from "./components/EmptyState";
import { AuthProvider, useAuth } from "./context/auth";
import { Gate } from "./pages/Gate";
import { Person } from "./pages/Person";
import { Review } from "./pages/Review";
import { Search } from "./pages/Search";
import { Shortlists } from "./pages/Shortlists";
import { Sources } from "./pages/Sources";

/** Hash routing, not history routing. The backend serves this app from a
 *  single `/ui` route with no rewrite rule behind it, so a real path like
 *  /person/abc would 404 on reload. Hash URLs also keep every link anyone has
 *  already saved from the previous UI working unchanged. */
function Routed() {
  const { token, required } = useAuth();
  // wait for the answer rather than flashing the gate at an open backend
  if (required === null) return <Loading message="Starting Seekr…" />;
  if (required && !token) return <Gate />;
  return (
    <Routes>
      <Route path="/search" element={<Search />} />
      <Route path="/person/:id" element={<Person />} />
      <Route path="/shortlists" element={<Shortlists />} />
      <Route path="/review" element={<Review />} />
      <Route path="/sources" element={<Sources />} />
      <Route path="*" element={<Navigate to="/search" replace />} />
    </Routes>
  );
}

export function App() {
  return (
    <AuthProvider>
      <Router>
        <Routed />
      </Router>
    </AuthProvider>
  );
}
