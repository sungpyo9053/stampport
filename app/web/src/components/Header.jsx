import { useApp } from '../context/appContext.js';

const PROVIDER_LABEL = {
  guest: 'GUEST',
  kakao: 'KAKAO',
  naver: 'NAVER',
};

export default function Header({ navigate }) {
  const { user, logout, level, selectedTitle } = useApp();

  return (
    <header className="app-header">
      <button
        type="button"
        className="brand"
        onClick={() => navigate(user ? '/passport' : '/')}
      >
        <span className="brand-mark">SP</span>
        <span className="brand-text">STAMPPORT</span>
      </button>

      <div className="header-actions">
        {user ? (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <button
              type="button"
              className="identity-chip"
              onClick={() => navigate('/passport')}
              aria-label="내 여권"
              style={{ appearance: 'none', cursor: 'pointer' }}
            >
              <span className="avatar">
                {(user.nickname || '?').slice(0, 1)}
              </span>
              <span style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', lineHeight: 1.1 }}>
                <span>Lv.{level} · {user.nickname}</span>
                <span className="meta">
                  {selectedTitle || user.passport_title || '동네 도장 수집가'}
                  {' · '}
                  {PROVIDER_LABEL[user.provider] || 'GUEST'}
                </span>
              </span>
            </button>
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
          </div>
        ) : (
          <button type="button" className="icon-btn" onClick={() => navigate('/login')}>
            로그인
          </button>
        )}
      </div>
    </header>
  );
}
