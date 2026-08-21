package auth

import (
	"context"
	"time"

	"github.com/redis/go-redis/v9"
)

// RedisDenylist — отзыв токенов по jti (Гл. 9.2.2). Ключи живут ровно до
// истечения самого токена, поэтому список не растёт бесконечно.
type RedisDenylist struct {
	rdb    *redis.Client
	prefix string
}

func NewRedisDenylist(addr, password string) *RedisDenylist {
	if addr == "" {
		return nil
	}
	return &RedisDenylist{
		rdb:    redis.NewClient(&redis.Options{Addr: addr, Password: password}),
		prefix: "jwt:revoked:",
	}
}

func (d *RedisDenylist) Revoked(ctx context.Context, jti string) (bool, error) {
	n, err := d.rdb.Exists(ctx, d.prefix+jti).Result()
	return n > 0, err
}

func (d *RedisDenylist) Revoke(ctx context.Context, jti string, ttl time.Duration) error {
	return d.rdb.Set(ctx, d.prefix+jti, "1", ttl).Err()
}

func (d *RedisDenylist) Close() error { return d.rdb.Close() }
