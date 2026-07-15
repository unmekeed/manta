import { Link, Outlet } from "react-router-dom";

export default function App() {
  return (
    <div className="layout">
      <header className="topbar">
        <Link to="/" className="brand">
          Dota <span>AI</span> Analyst
        </Link>
        <span className="tagline">разбор матчей · Win Probability · ошибки</span>
      </header>
      <main>
        <Outlet />
      </main>
    </div>
  );
}
