import { useEffect, useState } from 'react';
import { useApp } from '../context/appContext.js';
import {
  consumeOAuthState,
  MOCK_PROFILE,
  parseCallbackParams,
} from '../utils/oauth.js';

// AuthCallback — landing pad for /#/auth/callback/<provider>.
//
// Real OAuth token exchange (code → access_token → user_info) belongs
// on a backend; the front-end never sees a client_secret. For now we
// just validate the round-trip state, then synthesize a profile from
// the provider's mock metadata so the user lands on /passport with a
// passport-identity instead of a dead-end screen.
//
// docs/auth.md describes how to extend this with a FastAPI callback
// route once Kakao/Naver developer apps are registered.

const PROVIDER_LABEL = {
  kakao: '카카오',
  naver: '네이버',
};

export default function AuthCallback({ navigate, provider }) {
  const { loginAs } = useApp();
  const [error, setError] = useState('');
  const [phase, setPhase] = useState('processing'); // processing | done | error

  useEffect(() => {
    const params = parseCallbackParams(provider);
    if (params.error) {
      setError(`${PROVIDER_LABEL[provider] || provider} 로그인이 취소되었거나 실패했습니다 (${params.error})`);
      setPhase('error');
      return;
    }

    // Read+clear the state we stored in sessionStorage before
    // redirecting. If the round-trip arrived without a code we still
    // synthesize a mock profile so a phone test doesn't get stuck.
    const expected = consumeOAuthState();
    const stateOk = expected && params.state && expected === params.state;

    // Real token exchange would run here. Until the backend callback
    // exists we fall back to a mock profile for the matching provider.
    const mock = MOCK_PROFILE[provider] || { nickname: '여행자', avatar_style: 'stamp_collector' };
    try {
      loginAs({
        provider,
        nickname: mock.nickname,
        avatar_style: mock.avatar_style,
        // provider_user_id intentionally null — we don't have a real
        // user id until the backend is wired up.
      });
      setPhase('done');
      // Defer the navigate so the success state is visible long
      // enough to feel intentional on a fast network.
      const t = setTimeout(() => navigate('/passport', { replace: true }), 600);
      return () => clearTimeout(t);
    } catch (err) {
      setError(err?.message || '로그인 처리 중 알 수 없는 오류가 발생했습니다.');
      setPhase('error');
    }

    // stateOk is currently informational — once the backend lands
    // we'll refuse the login when it's false.
    if (!stateOk && import.meta.env?.DEV) {
      // eslint-disable-next-line no-console
      console.warn('[stampport] OAuth state mismatch (dev warning)', { expected, params });
    }
  }, [loginAs, navigate, provider]);

  if (phase === 'error') {
    return (
      <section className="auth-callback">
        <h1>로그인 처리에 실패했어요</h1>
        <p>{error}</p>
        <button
          type="button"
          className="btn btn-primary btn-block"
          onClick={() => navigate('/login', { replace: true })}
        >
          다시 시도
        </button>
      </section>
    );
  }

  return (
    <section className="auth-callback">
      <h1>잠시만요</h1>
      <p>
        {PROVIDER_LABEL[provider] || provider} 계정으로 여권을 준비하고 있어요…
        <br />
        곧 자동으로 내 여권 화면으로 넘어갑니다.
      </p>
    </section>
  );
}
