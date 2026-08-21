package config

import (
	"strings"
	"testing"
)

func TestDevModeKeepsOptionalSecurity(t *testing.T) {
	cfg := Config{}
	if err := cfg.Validate(); err != nil {
		t.Fatalf("dev config should remain valid: %v", err)
	}
}

func TestProductionRejectsEveryIncompleteSecurityConfig(t *testing.T) {
	complete := Config{
		Production:       true,
		JWTPublicKeyFile: "/run/secrets/jwt-public.pem",
		TLSCertFile:      "/run/secrets/tls-cert.pem",
		TLSKeyFile:       "/run/secrets/tls-key.pem",
	}
	tests := []struct {
		name string
		edit func(*Config)
		want string
	}{
		{"no jwt", func(c *Config) { c.JWTPublicKeyFile = "" }, "JWT_PUBLIC_KEY_FILE"},
		{"no cert", func(c *Config) { c.TLSCertFile = "" }, "TLS_CERT_FILE"},
		{"no tls key", func(c *Config) { c.TLSKeyFile = "" }, "TLS_KEY_FILE"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			cfg := complete
			tt.edit(&cfg)
			err := cfg.Validate()
			if err == nil || !strings.Contains(err.Error(), tt.want) {
				t.Fatalf("expected %s refusal, got %v", tt.want, err)
			}
		})
	}
	if err := complete.Validate(); err != nil {
		t.Fatalf("complete production config rejected: %v", err)
	}
}

func TestLoadTreatsEveryNonzeroProductionValueAsFailClosed(t *testing.T) {
	for value, want := range map[string]bool{"": false, "0": false, "true": true, "1": true} {
		t.Run(value, func(t *testing.T) {
			t.Setenv("MANTA_PROD", value)
			if got := Load().Production; got != want {
				t.Fatalf("MANTA_PROD=%q: got %v, want %v", value, got, want)
			}
		})
	}
}
