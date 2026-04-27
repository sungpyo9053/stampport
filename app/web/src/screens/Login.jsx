import { useMemo, useState } from 'react';
import { useApp } from '../context/appContext.js';
import {
  buildAuthorizeUrl,
  isOAuthConfigured,
  MOCK_PROFILE,
} from '../utils/oauth.js';
import { loadStamps, loadUser } from '../utils/storage.js';
import { levelFor, totalExp } from '../utils/leveling.js';

// Login screen — Stampport's "내 로컬 여권 시작" entry. Social
// (Kakao/Naver) is the primary CTA; nickname-only guest mode is the
// secondary path. When real OAuth client ids aren't configured we
// fall back to a mock social login so a fresh-clone install can
// still sign a profile in.
//
// Identity rules:
//   - real OAuth → redirect to provider; the AuthCallback screen
//     finishes the login (or, today, just synthesizes a profile).
//   - mock OAuth → loginAs(provider, mockNickname) right here.
//   - guest     → loginAs("guest", typedNickname).

const PROVIDER_LABEL = {
  kakao: '카카오',
  naver: '네이버',
};

function SocialButton({ provider, onClick, configured, isMock }) {
  const label = PROVIDER_LABEL[provider] || provider;
  const palette = provider === 'kakao'
    ? { bg: '#FEE500', fg: '#191600', border: '#E0CC00' }
    : { bg: '#03C75A', fg: '#FFFFFF', border: '#02A14B' };
  return (
    <button
      type="button"
      className="btn btn-block"
      onClick={onClick}
      style={{
        backgroundColor: palette.bg,
        color: palette.fg,
        border: `1px solid ${palette.border}`,
        fontWeight: 700,
      }}
    >
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
        <span aria-hidden="true">{provider === 'kakao' ? '💬' : 'N'}</span>
        {label}로 시작하기
        {!configured && (
          <span
            style={{
              fontSize: 10,
              fontWeight: 700,
              opacity: 0.8,
              padding: '2px 6px',
              borderRadius: 999,
              backgroundColor: 'rgba(0,0,0,0.12)',
              marginLeft: 4,
            }}
          >
            {isMock ? 'DEV' : ''}
          </span>
        )}
      </span>
    </button>
  );
}

export default function Login({ navigate }) {
  const { loginAs } = useApp();
  const [showGuestForm, setShowGuestForm] = useState(false);
  const [nickname, setNickname] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  // Returning-passport hint — if there's a profile in localStorage,
  // show a "이어가기" panel so the player feels their character is
  // waiting, instead of being asked to start over.
  const returning = useMemo(() => {
    const u = loadUser();
    if (!u) return null;
    const stamps = loadStamps(u.user_id);
    const exp = totalExp(stamps);
    return {
      nickname: u.nickname || '여행자',
      provider: u.provider || 'guest',
      level: levelFor(exp),
      stamps: stamps.length,
      exp,
    };
  }, []);

  const handleSocial = (provider) => {
    setError('');
    if (busy) return;
    setBusy(true);
    try {
      const built = buildAuthorizeUrl(provider);
      if (!built.isMock && built.url) {
        // Real OAuth flow — leave the SPA. The provider will redirect
        // back to /auth/callback/<provider>. We persist a "we're
        // starting an OAuth round-trip" hint via sessionStorage in
        // rememberOAuthState (already called inside buildAuthorizeUrl).
        window.location.assign(built.url);
        return;
      }
      // Mock fallback — synthesize a profile so the app is fully
      // usable without OAuth keys configured.
      const mock = MOCK_PROFILE[provider] || { nickname: '여행자', avatar_style: 'stamp_collector' };
      loginAs({
        provider,
        nickname: mock.nickname,
        avatar_style: mock.avatar_style,
        // No provider_user_id — this is a dev/demo profile.
      });
      navigate('/passport');
    } finally {
      setBusy(false);
    }
  };

  const handleGuest = (event) => {
    event?.preventDefault?.();
    if (busy) return;
    const trimmed = nickname.trim();
    if (!trimmed) {
      setError('닉네임을 입력해 주세요.');
      return;
    }
    setError('');
    setBusy(true);
    try {
      loginAs({ provider: 'guest', nickname: trimmed });
      navigate('/passport');
    } finally {
      setBusy(false);
    }
  };

  const kakaoConfigured = isOAuthConfigured('kakao');
  const naverConfigured = isOAuthConfigured('naver');

  return (
    <section className="login">
      <div className="login-hero">
        <div className="passport-monogram" aria-hidden="true">
          <span>SP</span>
        </div>
        <h1>내 로컬 여권 시작하기</h1>
        <p>
          오늘부터 나만의 취향 여권을 만듭니다.
          <br />
          도장을 모으고, 배지를 얻고, 동네 칭호를 키워보세요.
        </p>
      </div>

      {returning ? (
        <div className="returning-card" role="status">
          <div className="rc-avatar" aria-hidden="true">
            {(returning.nickname || '?').slice(0, 1)}
          </div>
          <div className="rc-text">
            <div className="rc-line">
              <strong>{returning.nickname}</strong>
              <span className="rc-pip">Lv.{returning.level}</span>
            </div>
            <div className="rc-meta">
              도장 {returning.stamps}개 · 누적 {returning.exp} EXP ·{' '}
              {(returning.provider || 'guest').toUpperCase()} 여권
            </div>
            <div className="rc-hint">
              여권이 이 기기에 저장돼 있어요. 같은 방법으로 로그인하면 그대로 이어집니다.
            </div>
          </div>
        </div>
      ) : null}

      <ul className="login-perks">
        <li><span aria-hidden="true">📓</span> 내 취향 여권</li>
        <li><span aria-hidden="true">🟫</span> 방문 도장</li>
        <li><span aria-hidden="true">🏅</span> 지역 배지</li>
        <li><span aria-hidden="true">🏷️</span> 칭호 / 레벨</li>
        <li><span aria-hidden="true">✨</span> 공유 카드</li>
      </ul>

      <div className="form-stack login-cta">
        <SocialButton
          provider="kakao"
          configured={kakaoConfigured}
          isMock={!kakaoConfigured}
          onClick={() => handleSocial('kakao')}
        />
        <SocialButton
          provider="naver"
          configured={naverConfigured}
          isMock={!naverConfigured}
          onClick={() => handleSocial('naver')}
        />

        <button
          type="button"
          className="btn btn-ghost btn-block"
          onClick={() => setShowGuestForm((v) => !v)}
        >
          게스트로 둘러보기
        </button>

        {(!kakaoConfigured || !naverConfigured) && (
          <p className="form-helper login-mock-note">
            {kakaoConfigured && naverConfigured
              ? null
              : '소셜 로그인은 환경변수(VITE_KAKAO_CLIENT_ID / VITE_NAVER_CLIENT_ID)가 설정되지 않아 데모 모드로 동작합니다. 여권은 이 기기에 저장됩니다.'}
          </p>
        )}
      </div>

      {showGuestForm && (
        <form className="form-stack" onSubmit={handleGuest}>
          <div className="form-field">
            <label htmlFor="nickname">닉네임</label>
            <input
              id="nickname"
              type="text"
              value={nickname}
              onChange={(e) => setNickname(e.target.value)}
              placeholder="예: 빵지순례러"
              maxLength={20}
              autoComplete="nickname"
            />
            <span className="form-helper">
              게스트로 시작해도 여권은 이 기기에 저장됩니다. 나중에 소셜 계정 연결로
              이어갈 수 있어요.
            </span>
          </div>

          {error && (
            <p className="form-helper" style={{ color: 'var(--color-burgundy)' }}>
              {error}
            </p>
          )}

          <button type="submit" className="btn btn-primary btn-block" disabled={busy}>
            여권 만들고 시작하기
          </button>
        </form>
      )}

      <button
        type="button"
        className="btn btn-ghost btn-block"
        onClick={() => navigate('/')}
        style={{ marginTop: 12 }}
      >
        돌아가기
      </button>
    </section>
  );
}
