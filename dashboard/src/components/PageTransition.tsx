import React, { ReactNode, useEffect, useState, useRef } from 'react';
import { useLocation, useNavigationType } from 'react-router';
import { cn } from '@/lib/utils';

interface PageTransitionProps {
  children: ReactNode;
  duration?: number; // in milliseconds
  delay?: number; // in milliseconds
}

export default function PageTransition({ 
  children, 
  duration = 300,
  delay = 0
}: PageTransitionProps) {
  const location = useLocation();
  const navigationType = useNavigationType();
  const [displayChildren, setDisplayChildren] = useState(children);
  const [isPageTransitioning, setIsPageTransitioning] = useState(false);
  const [isShaking, setIsShaking] = useState(false);
  const previousPathnameRef = useRef(location.pathname);
  const isFirstRenderRef = useRef(true);
  const hasNavigatedRef = useRef(false);

  // Update children when they change
  useEffect(() => {
    if (!isShaking) {
      setDisplayChildren(children);
    }
  }, [children, isShaking]);

  // Handle navigation effects
  useEffect(() => {
    // Skip on first render
    if (isFirstRenderRef.current) {
      isFirstRenderRef.current = false;
      return;
    }

    // Skip animations on browser actions like refresh (POP)
    if (navigationType === 'POP') {
      setDisplayChildren(children);
      previousPathnameRef.current = location.pathname;
      return;
    }

    // Same page navigation - trigger shake animation
    // Only if we've already navigated before (prevent after login)
    if (location.pathname === previousPathnameRef.current && hasNavigatedRef.current) {
      setIsShaking(true);
      
      // Reset shake after animation completes
      setTimeout(() => {
        setIsShaking(false);
      }, duration);
      
      return;
    }

    // Different page navigation - fade transition
    setIsPageTransitioning(true);
    
    // Wait for fade-out, then update content
    setTimeout(() => {
      setDisplayChildren(children);
      setIsPageTransitioning(false);
      previousPathnameRef.current = location.pathname;
      hasNavigatedRef.current = true;
    }, 150);
    
  }, [location, navigationType, children, duration]);

  return (
    <div
      className={cn(
        'w-full transform-gpu will-change-transform overflow-x-visible',
        isShaking ? 'animate-telegram-shake' : '',
        isPageTransitioning ? 'opacity-0 translate-y-2' : 'opacity-100 translate-y-0'
      )}
      style={{ 
        animationDuration: isShaking ? `${duration}ms` : undefined,
        animationDelay: isShaking && delay > 0 ? `${delay}ms` : undefined,
        animationFillMode: isShaking ? 'both' : undefined,
        transition: 'opacity 150ms ease, transform 150ms ease',
      }}
    >
      {displayChildren}
    </div>
  );
} 