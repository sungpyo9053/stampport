import { useApp } from '../context/appContext.js';

export default function Header({ navigate }) {
  const { user, logout } = useApp();

  return (
    <header className="app-header">
      <button
        type="button"
        className="brand"
        onClick={() => navigate(user ? '/passport' : '/')}
      >
        <span className="brand-mark">SP</span>
        스탬포트
      </button>
      <div className="header-actions">
        {user ? (
          <button
            type="button"
            className="icon-btn"
            onClick={() => {
              logout();
              navigate('/');
            }}
          >
            로그아웃
          </button>
        ) : (
          <button type="button" className="icon-btn" onClick={() => navigate('/login')}>
            로그인
          </button>
        )}
      </div>
    </header>
  );
}
