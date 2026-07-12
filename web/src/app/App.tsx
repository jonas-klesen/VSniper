import { NavLink, Outlet } from 'react-router-dom';
import {
  BarChart3,
  Cpu,
  Gauge,
  HardDrive,
  LayoutGrid,
  Search,
  Settings,
  User,
} from 'lucide-react';

const links = [
  { to: '/', label: 'Dashboard', icon: Gauge, end: true },
  { to: '/my-taste', label: 'My Taste', icon: User },
  { to: '/searches', label: 'Searches', icon: Search },
  { to: '/candidates', label: 'Candidates', icon: LayoutGrid },
  { to: '/costs', label: 'AI Costs', icon: BarChart3 },
  { to: '/ai-models', label: 'AI Models', icon: Cpu },
  { to: '/settings', label: 'Settings', icon: Settings },
  { to: '/storage', label: 'Storage', icon: HardDrive },
];

export function AppLayout() {
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div>
          <p className="eyebrow">vsniper</p>
          <h1>Vinted Operations</h1>
          <p className="muted">Search, scoring, alerts, and review.</p>
        </div>
        <nav className="nav-list">
          {links.map((link) => (
            <NavLink
              key={link.to}
              to={link.to}
              end={link.end}
              className={({ isActive }) => (isActive ? 'nav-link active' : 'nav-link')}
            >
              <link.icon size={16} />
              {link.label}
            </NavLink>
          ))}
        </nav>
      </aside>
      <main className="content">
        <Outlet />
      </main>
    </div>
  );
}
