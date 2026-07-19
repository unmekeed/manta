import { Link, Outlet } from "react-router-dom";

export default function App() {
  return (
    <div className="layout">
      <header className="topbar">
        <Link to="/" className="brand">
          Man<span>ta</span>
        </Link>
        <nav className="nav">
          <Link to="/">Матчи</Link>
          <Link to="/draft">Драфт</Link>
        </nav>
        <span className="tagline">аналитика Dota 2 · Win Probability · разбор ошибок</span>
      </header>
      <main>
        <Outlet />
      </main>
    </div>
  );
}
