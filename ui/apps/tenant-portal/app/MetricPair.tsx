import styles from "./ledger.module.css";
import { phanTram } from "@/lib/competency";

/**
 * Độ chính xác và độ phủ, LUÔN đi cùng nhau và LUÔN ngang hàng.
 *
 * Đây không phải lựa chọn thẩm mỹ. Hai số này kéo ngược nhau: muốn độ chính xác
 * cao nhất thì chỉ cần từ chối mọi ca khó — trông rất cẩn thận, thực chất vô
 * dụng. Vì vậy không có API nào ở component này cho phép hiện một số mà giấu số
 * kia, và hai ô dùng chung đúng một class.
 */
export function MetricPair({
  accuracy,
  coverage,
  accuracyNote,
  coverageNote,
  footer,
}: {
  accuracy: number;
  coverage: number;
  accuracyNote?: string;
  coverageNote?: string;
  footer?: string;
}) {
  return (
    <div className={styles.duo}>
      <div className={styles.duoCell}>
        <div className={styles.duoLabel}>Độ tin cậy tối thiểu</div>
        <div className={styles.duoValue}>{phanTram(accuracy)}</div>
        {accuracyNote ? <div className={styles.duoNote}>{accuracyNote}</div> : null}
      </div>
      <div className={styles.duoCell}>
        <div className={styles.duoLabel}>Độ phủ</div>
        <div className={styles.duoValue}>{phanTram(coverage)}</div>
        {coverageNote ? <div className={styles.duoNote}>{coverageNote}</div> : null}
      </div>
      {footer ? <div className={styles.duoLink}>{footer}</div> : null}
    </div>
  );
}
