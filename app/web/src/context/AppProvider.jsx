import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  clearUser,
  loadProfileMeta,
  loadStamps,
  loadUser,
  saveProfileMeta,
  saveStamps,
  saveUser,
} from '../utils/storage.js';
import { computeBadges, BADGE_DEFS } from '../data/badges.js';
import { computeQuests } from '../data/quests.js';
import { expGainFor, levelFor, levelProgress, totalExp } from '../utils/leveling.js';
import { generateKickPoints } from '../utils/kickPoints.js';
import { AppContext } from './appContext.js';

function makeUserId(email) {
  const base = (email || '').toLowerCase().trim() || `guest-${Date.now()}`;
  return `u_${base.replace(/[^a-z0-9]/g, '_')}`;
}

function makeStampId() {
  return `s_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

export function AppProvider({ children }) {
  const [user, setUser] = useState(() => loadUser());
  const [stamps, setStamps] = useState(() => {
    const u = loadUser();
    return u ? loadStamps(u.user_id) : [];
  });
  const [profileMeta, setProfileMeta] = useState(() => {
    const u = loadUser();
    return u ? loadProfileMeta(u.user_id) : { selected_title_id: null };
  });

  useEffect(() => {
    if (user) saveUser(user);
  }, [user]);

  useEffect(() => {
    if (user) saveStamps(user.user_id, stamps);
  }, [user, stamps]);

  useEffect(() => {
    if (user) saveProfileMeta(user.user_id, profileMeta);
  }, [user, profileMeta]);

  const login = useCallback(({ nickname, email }) => {
    const userId = makeUserId(email);
    const existingUser = loadUser();
    let nextUser;
    if (existingUser && existingUser.user_id === userId) {
      nextUser = { ...existingUser, nickname, email };
    } else {
      nextUser = {
        user_id: userId,
        nickname,
        email,
        created_at: new Date().toISOString(),
      };
    }
    setUser(nextUser);
    setStamps(loadStamps(userId));
    setProfileMeta(loadProfileMeta(userId));
    saveUser(nextUser);
    return nextUser;
  }, []);

  const logout = useCallback(() => {
    clearUser();
    setUser(null);
    setStamps([]);
    setProfileMeta({ selected_title_id: null });
  }, []);

  const addStamp = useCallback(
    (input) => {
      const previous = stamps;
      const visited_at = input.visited_at || new Date().toISOString().slice(0, 10);
      const stamp = {
        id: makeStampId(),
        user_id: user?.user_id,
        place_name: input.place_name?.trim() || '',
        area: input.area || '기타',
        category: input.category || 'cafe',
        tags: input.tags || [],
        representative_menu: input.representative_menu?.trim() || '',
        visited_at,
        verification_level: input.verification_level || 'manual',
        verification_status: 'unverified',
        trust_score: 0,
        created_at: new Date().toISOString(),
      };
      stamp.kick_points = generateKickPoints(stamp);
      const exp_gained = expGainFor(stamp, previous);
      stamp.exp_gained = exp_gained;
      setStamps([stamp, ...previous]);
      return stamp;
    },
    [stamps, user],
  );

  const setSelectedTitle = useCallback((badgeId) => {
    setProfileMeta((prev) => ({ ...prev, selected_title_id: badgeId }));
  }, []);

  const exp = useMemo(() => totalExp(stamps), [stamps]);
  const level = useMemo(() => levelFor(exp), [exp]);
  const levelInfo = useMemo(() => levelProgress(exp), [exp]);
  const badges = useMemo(() => computeBadges(stamps), [stamps]);
  const quests = useMemo(() => computeQuests(stamps), [stamps]);

  const earnedBadges = useMemo(() => badges.filter((b) => b.earned), [badges]);

  const selectedTitle = useMemo(() => {
    const id = profileMeta.selected_title_id;
    if (id) {
      const def = BADGE_DEFS.find((d) => d.id === id);
      const earned = earnedBadges.find((b) => b.id === id);
      if (def && earned) return def.titleLabel;
    }
    if (earnedBadges.length > 0) {
      const def = BADGE_DEFS.find((d) => d.id === earnedBadges[earnedBadges.length - 1].id);
      return def?.titleLabel || '로컬 미식 새내기';
    }
    return '로컬 미식 새내기';
  }, [profileMeta, earnedBadges]);

  const stampById = useCallback(
    (id) => stamps.find((s) => s.id === id) || null,
    [stamps],
  );

  const value = useMemo(
    () => ({
      user,
      stamps,
      addStamp,
      login,
      logout,
      exp,
      level,
      levelInfo,
      badges,
      earnedBadges,
      quests,
      selectedTitle,
      profileMeta,
      setSelectedTitle,
      stampById,
    }),
    [
      user,
      stamps,
      addStamp,
      login,
      logout,
      exp,
      level,
      levelInfo,
      badges,
      earnedBadges,
      quests,
      selectedTitle,
      profileMeta,
      setSelectedTitle,
      stampById,
    ],
  );

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}
