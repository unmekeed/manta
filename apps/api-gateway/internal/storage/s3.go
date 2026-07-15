package storage

import (
	"context"
	"fmt"
	"io"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
)

// ReplayStore выгружает файлы реплеев в S3-совместимое хранилище (Гл. 2.6.1).
type ReplayStore struct {
	client *minio.Client
	bucket string
}

func NewReplayStore(endpoint, accessKey, secretKey, bucket string, useSSL bool) (*ReplayStore, error) {
	client, err := minio.New(endpoint, &minio.Options{
		Creds:  credentials.NewStaticV4(accessKey, secretKey, ""),
		Secure: useSSL,
	})
	if err != nil {
		return nil, fmt.Errorf("minio client: %w", err)
	}
	return &ReplayStore{client: client, bucket: bucket}, nil
}

// EnsureBucket создаёт бакет, если он отсутствует (идемпотентно).
func (s *ReplayStore) EnsureBucket(ctx context.Context) error {
	exists, err := s.client.BucketExists(ctx, s.bucket)
	if err != nil {
		return fmt.Errorf("bucket exists check: %w", err)
	}
	if !exists {
		if err := s.client.MakeBucket(ctx, s.bucket, minio.MakeBucketOptions{}); err != nil {
			return fmt.Errorf("make bucket: %w", err)
		}
	}
	return nil
}

// PutReplay сохраняет поток реплея под ключом objectKey и возвращает S3-URL.
func (s *ReplayStore) PutReplay(ctx context.Context, objectKey string, r io.Reader, size int64) (string, error) {
	_, err := s.client.PutObject(ctx, s.bucket, objectKey, r, size, minio.PutObjectOptions{
		ContentType: "application/octet-stream",
	})
	if err != nil {
		return "", fmt.Errorf("put object: %w", err)
	}
	return fmt.Sprintf("s3://%s/%s", s.bucket, objectKey), nil
}
