const TABS = [
  { path: '/passport', label: '여권', icon: '📓' },
  { path: '/stamp', label: '스탬프', icon: '🖋' },
  { path: '/badges', label: '뱃지', icon: '🏅' },
  { path: '/quests', label: '퀘스트', icon: '🎯' },
];

export default function TabBar({ path, navigate }) {
  return (
    <nav className="app-tabbar" aria-label="주 메뉴">
      {TABS.map((tab) => (
        <button
          key={tab.path}
          type="button"
          className={path.startsWith(tab.path) ? 'active' : ''}
          onClick={() => navigate(tab.path)}
        >
          <span className="tab-icon" aria-hidden="true">
            {tab.icon}
          </span>
          {tab.label}
        </button>
      ))}
    </nav>
  );
}
