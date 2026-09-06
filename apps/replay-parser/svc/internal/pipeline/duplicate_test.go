package pipeline

import (
	"errors"
	"fmt"
	"net/http"
	"testing"

	"github.com/minio/minio-go/v7"
)

// Различение «дубликата» и «потери реплея» (спринт 194).
//
// Разобранный реплей удаляется из S3, а доставка событий at-least-once по
// контракту. Значит повторное событие того же матча законно приходит к
// пустому месту. До 194-го это давало ERROR и уход в DLQ — то есть ровно
// то же, что настоящая потеря реплея, которую надо спасать. Одинаковый
// вид у нормы и у беды приучает пропускать обе.

func TestMissingObjectIsRecognisedByCodeNotText(t *testing.T) {
	// ПОЧЕМУ ПО КОДУ. Текст ошибки — собственность чужой библиотеки: он
	// меняется между версиями молча, и разбор по подстроке однажды
	// перестанет находить, не сломав ни одной сборки.
	p := &Pipeline{}
	missing := minio.ErrorResponse{Code: "NoSuchKey",
		Message: "The specified key does not exist."}
	if !p.IsMissingObject(missing) {
		t.Error("NoSuchKey не распознан")
	}
	wrapped := fmt.Errorf("s3 get replays/x.dem: %w", missing)
	if !p.IsMissingObject(wrapped) {
		t.Error("NoSuchKey не найден под обёрткой fmt.Errorf")
	}
	byStatus := minio.ErrorResponse{StatusCode: http.StatusNotFound}
	if !p.IsMissingObject(byStatus) {
		t.Error("404 без кода не распознан")
	}
}

func TestOtherFailuresAreNotMistakenForAMissingObject(t *testing.T) {
	// Недоступное хранилище, отказ в доступе, обрыв — всё это НЕ
	// «объекта нет». Спутав их, мы проглотили бы настоящий отказ как
	// безобидный дубликат.
	p := &Pipeline{}
	for _, err := range []error{
		errors.New("connection refused"),
		minio.ErrorResponse{Code: "AccessDenied", StatusCode: 403},
		minio.ErrorResponse{Code: "InternalError", StatusCode: 500},
		nil,
	} {
		if p.IsMissingObject(err) {
			t.Errorf("ошибка %v принята за отсутствующий объект", err)
		}
	}
}

func TestWithoutClickhouseNothingIsDeclaredParsed(t *testing.T) {
	// Нет клиента — нет ответа на вопрос «разобран ли матч», и ответ
	// обязан быть «не знаю» → false. Иначе отсутствие связи с витриной
	// превращало бы КАЖДУЮ потерю реплея в тихий дубликат.
	p := &Pipeline{}
	if p.AlreadyParsed(nil, 12345) {
		t.Error("без ClickHouse матч объявлен разобранным")
	}
}
