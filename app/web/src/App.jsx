import { useEffect } from 'react';
import './App.css';
import Header from './components/Header.jsx';
import TabBar from './components/TabBar.jsx';
import Landing from './screens/Landing.jsx';
import Login from './screens/Login.jsx';
import StampForm from './screens/StampForm.jsx';
import StampResult from './screens/StampResult.jsx';
import MyPassport from './screens/MyPassport.jsx';
import Badges from './screens/Badges.jsx';
import Quests from './screens/Quests.jsx';
import Share from './screens/Share.jsx';
import { useApp } from './context/appContext.js';
import { useHashRoute } from './utils/router.js';

const PROTECTED_PREFIXES = ['/stamp', '/result', '/passport', '/badges', '/quests', '/share'];

function isProtected(path) {
  return PROTECTED_PREFIXES.some((p) => path === p || path.startsWith(`${p}/`));
}

function pickRoute(path) {
  if (path === '/' || path === '/landing' || path === '') {
    return { name: 'landing' };
  }
  if (path === '/login') return { name: 'login' };
  if (path === '/stamp') return { name: 'stamp' };
  if (path.startsWith('/result/')) {
    return { name: 'result', stampId: path.slice('/result/'.length) };
  }
  if (path === '/passport') return { name: 'passport' };
  if (path === '/badges') return { name: 'badges' };
  if (path === '/quests') return { name: 'quests' };
  if (path.startsWith('/share/')) {
    return { name: 'share', stampId: path.slice('/share/'.length) };
  }
  return { name: 'landing' };
}

const HIDE_TABBAR = new Set(['landing', 'login', 'result', 'share']);

export default function App() {
  const { user } = useApp();
  const { path, navigate } = useHashRoute();

  useEffect(() => {
    if (!user && isProtected(path)) {
      navigate('/login', { replace: true });
    }
    if (user && (path === '/' || path === '' || path === '/landing')) {
      navigate('/passport', { replace: true });
    }
  }, [user, path, navigate]);

  const route = pickRoute(path);

  let screen;
  switch (route.name) {
    case 'landing':
      screen = <Landing navigate={navigate} />;
      break;
    case 'login':
      screen = <Login navigate={navigate} />;
      break;
    case 'stamp':
      screen = user ? <StampForm navigate={navigate} /> : null;
      break;
    case 'result':
      screen = user ? <StampResult navigate={navigate} stampId={route.stampId} /> : null;
      break;
    case 'passport':
      screen = user ? <MyPassport navigate={navigate} /> : null;
      break;
    case 'badges':
      screen = user ? <Badges navigate={navigate} /> : null;
      break;
    case 'quests':
      screen = user ? <Quests navigate={navigate} /> : null;
      break;
    case 'share':
      screen = user ? <Share navigate={navigate} stampId={route.stampId} /> : null;
      break;
    default:
      screen = <Landing navigate={navigate} />;
  }

  const showTabBar = user && !HIDE_TABBAR.has(route.name);
  const fullBleed = route.name === 'landing' || route.name === 'login';

  return (
    <div className="app-shell">
      <Header navigate={navigate} />
      {fullBleed ? screen : <main className="app-main">{screen}</main>}
      {showTabBar ? <TabBar path={path} navigate={navigate} /> : null}
    </div>
  );
}
